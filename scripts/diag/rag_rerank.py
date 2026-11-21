"""What reranking models would do with the passages the search already finds for each question of the kept test.

It reads the file rag_whatif.py wrote and changes nothing in HunterAI or in the collection. Each model is a
cross-encoder run by ONNX Runtime on the processor: it reads a question together with one passage and gives a score.
The passages of each question are put in the order of that score, and the test is counted again: how many questions
have their passage first, and in the message. Each model is tried twice: reading the passage alone, and reading the
name of its source above it. The models are downloaded once from Hugging Face into ~/.cache/rerank-bench.

From the backend folder:   uv run --with onnxruntime --with tokenizers --with numpy python - < rag_rerank.py
RERANK_MODELS: other models than the three Ettin sizes, comma separated (the names are in MODELS below).

A diagnostic for one decision, not a part of HunterAI. The scores were checked against the example scores the model
cards of the three Ettin rerankers print."""
import json
import multiprocessing
import os
import statistics
import struct
import sys
import time
import urllib.request
from pathlib import Path
from queue import Empty

from hunterai import config

# name -> where it is, its file, the tokens it is given at most here, megabytes to download, languages
MODELS = {
    "ettin-17m": {"repo": "cross-encoder/ettin-reranker-17m-v1", "file": "onnx/model.onnx", "max_len": 1024, "mb": 67, "head": True, "speaks": "English"},
    "ettin-32m": {"repo": "cross-encoder/ettin-reranker-32m-v1", "file": "onnx/model.onnx", "max_len": 1024, "mb": 128, "head": True, "speaks": "English"},
    "ettin-68m": {"repo": "cross-encoder/ettin-reranker-68m-v1", "file": "onnx/model.onnx", "max_len": 1024, "mb": 273, "head": True, "speaks": "English"},
    "minilm-l6": {"repo": "cross-encoder/ms-marco-MiniLM-L6-v2", "file": "onnx/model.onnx", "max_len": 512, "mb": 91, "speaks": "English"},
    "mmarco-l12": {"repo": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1", "file": "onnx/model.onnx", "max_len": 512, "mb": 471, "speaks": "many languages"},
    "bge-v2-m3-int8": {"repo": "onnx-community/bge-reranker-v2-m3-ONNX", "file": "onnx/model_int8.onnx", "max_len": 1024, "mb": 571, "speaks": "many languages"},
}
ASKED = [m.strip() for m in (os.environ.get("RERANK_MODELS") or "ettin-17m,ettin-32m,ettin-68m").split(",") if m.strip()]
CACHE = Path.home() / ".cache" / "rerank-bench"
HEAD_FILES = ["2_Dense/model.safetensors", "3_LayerNorm/model.safetensors", "4_Dense/model.safetensors"]
BATCH = 4
FIRST = 20                      # "the first 20": the cheaper thing a search could do
FORMS = (("text", "reads the passage"), ("named", "reads its source and the passage"))


def fetch(repo, rel):
    dst = CACHE / repo.replace("/", "__") / rel
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    print(f"  downloading {repo}/{rel} ...", file=sys.stderr, flush=True)
    req = urllib.request.Request(f"https://huggingface.co/{repo}/resolve/main/{rel}", headers={"User-Agent": "rerank-bench/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as fh:
        while block := r.read(1 << 20):
            fh.write(block)
    tmp.rename(dst)
    return dst


def read_safetensors(path):
    import numpy as np
    raw = Path(path).read_bytes()
    n = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8:8 + n])
    kinds = {"F32": np.float32, "F16": np.float16, "F64": np.float64}
    return {name: np.frombuffer(raw[8 + n + meta["data_offsets"][0]:8 + n + meta["data_offsets"][1]],
                                dtype=kinds[meta["dtype"]]).reshape(meta["shape"]).astype(np.float32)
            for name, meta in header.items() if name != "__metadata__"}


class ScoringHead:
    """The Ettin rerankers ship their ONNX file with the encoder only. The score comes from three small layers stored
    next to it: Dense(GELU) -> LayerNorm -> Dense(1), on the vector of the first token."""

    def __init__(self, folder):
        import math

        import numpy as np
        self.np = np
        d1, ln, d2 = (read_safetensors(Path(folder) / f) for f in HEAD_FILES)
        pick = lambda d, end: next(v for k, v in d.items() if k.endswith(end))
        self.w1, self.g, self.b = pick(d1, "weight"), pick(ln, "weight"), pick(ln, "bias")
        self.w2, self.b2 = pick(d2, "weight"), pick(d2, "bias")
        self.erf = np.vectorize(math.erf, otypes=[np.float32])

    def __call__(self, hidden):                         # (batch, tokens, dim)
        np = self.np
        x = hidden[:, 0, :] @ self.w1.T
        x = 0.5 * x * (1.0 + self.erf(x / np.sqrt(2.0)))                    # GELU
        mu, var = x.mean(-1, keepdims=True), x.var(-1, keepdims=True)
        x = (x - mu) / np.sqrt(var + 1e-5) * self.g + self.b                # LayerNorm
        return (x @ self.w2.T + self.b2)[:, 0]


class Reranker:
    """A cross-encoder in ONNX Runtime on the processor: a question and a passage in, one score out."""

    def __init__(self, name, threads):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        m = MODELS[name]
        onnx = fetch(m["repo"], m["file"])
        tok = fetch(m["repo"], "tokenizer.json")
        layers = [fetch(m["repo"], rel) for rel in HEAD_FILES] if m.get("head") else []     # all three are needed
        head = layers[0].parent.parent if layers else None
        self.np, self.max_len = np, m["max_len"]
        so = ort.SessionOptions()
        so.intra_op_num_threads, so.inter_op_num_threads = threads, 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = ort.InferenceSession(str(onnx), so, providers=["CPUExecutionProvider"])
        self.inputs = [i.name for i in self.sess.get_inputs()]
        self.head = ScoringHead(head) if head else None
        self.tok = Tokenizer.from_file(str(tok))
        self.tok.no_padding()
        self.tok.enable_truncation(max_length=self.max_len, strategy="only_second")
        self.plain = Tokenizer.from_file(str(tok))      # the same without a limit: to count what was cut
        self.plain.no_padding()
        self.plain.no_truncation()
        self.pad_id = next((self.tok.token_to_id(t) for t in ("<pad>", "[PAD]") if self.tok.token_to_id(t) is not None), 0)

    def cut(self, query, passages):
        """How many of these are longer than the model is given: their end is not read."""
        return sum(1 for e in self.plain.encode_batch([(query, p) for p in passages]) if len(e.ids) > self.max_len)

    def score(self, query, passages):
        np = self.np
        if not passages:
            return []
        enc = self.tok.encode_batch([(query, p) for p in passages])
        order = sorted(range(len(enc)), key=lambda i: len(enc[i].ids))      # alike in length side by side: less padding
        scores = [0.0] * len(enc)
        for s in range(0, len(order), BATCH):
            idx = order[s:s + BATCH]
            width = max(len(enc[i].ids) for i in idx)
            ids = np.full((len(idx), width), self.pad_id, dtype=np.int64)
            mask = np.zeros((len(idx), width), dtype=np.int64)
            types = np.zeros((len(idx), width), dtype=np.int64)
            for r, i in enumerate(idx):
                n = len(enc[i].ids)
                ids[r, :n], mask[r, :n], types[r, :n] = enc[i].ids, 1, enc[i].type_ids
            feed = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
            out = self.sess.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0]
            out = self.head(np.asarray(out, dtype=np.float32)) if self.head is not None else \
                np.asarray(out, dtype=np.float32).reshape(len(idx), -1)[:, -1]
            for r, i in enumerate(idx):
                scores[i] = float(out[r])
        return scores


def p_cores():
    """The fast cores of an Intel processor that has two kinds, or [] when there is one kind only."""
    try:
        text = Path("/sys/devices/cpu_core/cpus").read_text().strip()
    except OSError:
        return []
    cpus = []
    for part in text.split(","):
        a, _, b = part.partition("-")
        cpus += range(int(a), int(b or a) + 1)
    return cpus


def peak_mb():
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return 0.0


def judge(item, order, top, room):
    """(the place of the best passage that answers it, whether one that answers it is in the message), or None: as
    the search test counts."""
    sent, used = set(), 0
    for cid in order[:top]:                 # as a message is filled: whole passages while they fit
        if used + item["candidates"][cid]["cost"] > room:
            break
        used += item["candidates"][cid]["cost"]
        sent.add(cid)
    good = [place for place, cid in enumerate(order, 1) if cid in item["good"]]
    return (min(good), any(order[place - 1] in sent for place in good)) if good else None


def judged(data, orders):
    """For each question: (a passage that answers it stood first, one was in the message), for one order of its passages."""
    out = []
    for item, order in zip(data["items"], orders):
        got = judge(item, order, data["top"], data["room"])
        out.append((bool(got) and got[0] == 1, bool(got) and got[1]))
    return out


def count(data, results):
    """Per kind: (stood first, in the message)."""
    out = {}
    for item, (first, sent) in zip(data["items"], results):
        a, b = out.get(item["kind"], (0, 0))
        out[item["kind"]] = (a + int(first), b + int(sent))
    return out


def reordered(item, scores, which):
    """Passages that hold an identifier of the question in its exact spelling stay first, as the search puts them; the
    others by the model's score. `which`: the first FIRST of the search's order, the 40 it keeps, or all the two
    halves bring."""
    pins, now = item["pins"], [cid for cid in item["now"] if cid not in item["pins"]]
    by_score = lambda ids: sorted(ids, key=lambda cid: -scores[cid])
    if which == "first":
        return pins + by_score(now[:FIRST]) + now[FIRST:]
    if which == "kept":
        return pins + by_score(now)
    return pins + by_score([cid for cid in item["candidates"] if cid not in pins])


def read(candidate, form):
    """What the model is given of a passage: its text, or the name of its source and the text under it."""
    return candidate["source"] + "\n" + candidate["text"] if form == "named" else candidate["text"]


def one_model(name, data, threads, cpus, queue):
    """Runs in a process of its own, so that the memory it reports is this model's alone."""
    try:
        if cpus:
            os.sched_setaffinity(0, set(cpus))
        began = time.perf_counter()
        rr = Reranker(name, threads)
        loaded = time.perf_counter() - began
        rr.score("warm up", ["a passage", "another passage"])
        result = {"name": name, "loaded": loaded, "forms": {}}
        for form, _ in FORMS:
            orders = {"first": [], "kept": [], "all": []}
            seconds = {"first": [], "kept": [], "all": []}
            cut = pairs = 0
            for n, item in enumerate(data["items"], 1):
                if n % 10 == 0 or n == len(data["items"]):
                    print(f"  {name}, {dict(FORMS)[form]}: question {n} of {len(data['items'])}", file=sys.stderr, flush=True)
                cands = item["candidates"]
                now = item["now"]
                parts = [now[:FIRST], now[FIRST:], [cid for cid in cands if cid not in now]]
                scores, took = {}, []
                for ids in parts:
                    t0 = time.perf_counter()
                    scores.update(zip(ids, rr.score(item["query"], [read(cands[cid], form) for cid in ids])))
                    took.append(time.perf_counter() - t0)
                seconds["first"].append(took[0])
                seconds["kept"].append(took[0] + took[1])
                seconds["all"].append(sum(took))
                cut += rr.cut(item["query"], [read(c, form) for c in cands.values()])
                pairs += len(cands)
                for which, so_far in orders.items():
                    so_far.append(reordered(item, scores, which))
            result["forms"][form] = {"counts": {which: count(data, judged(data, orders[which])) for which in orders},
                                     "kept": judged(data, orders["kept"]),
                                     "seconds": {which: (statistics.median(s), max(s)) for which, s in seconds.items()},
                                     "cut": cut, "pairs": pairs}
        result["ram"] = peak_mb()
        queue.put(result)
    except Exception as e:                      # noqa: BLE001 - one model that fails must not end the others
        queue.put({"name": name, "error": f"{type(e).__name__}: {e}"[:300]})


def main():
    found = sorted((config.home() / "rag-tests").glob("*.candidates.json"), key=lambda p: p.stat().st_mtime)
    if not found:
        print("There is no candidates file yet. Run the what-if first: it writes one.")
        return 1
    data = json.loads(found[-1].read_text())
    data["items"] = data["items"][:int(os.environ.get("RERANK_LIMIT") or len(data["items"]))]
    unknown = [m for m in ASKED if m not in MODELS]
    if unknown:
        print(f"Unknown model(s): {', '.join(unknown)}. Known: {', '.join(MODELS)}.")
        return 1
    kinds = list(dict.fromkeys(item["kind"] for item in data["items"]))
    names = {"question": "questions", "code": "questions about code"}
    asked = {k: sum(1 for item in data["items"] if item["kind"] == k) for k in kinds}
    passages = sum(len(item["candidates"]) for item in data["items"])
    cpus = p_cores()
    threads = int(os.environ.get("RERANK_THREADS") or len(cpus) or os.cpu_count() or 1)
    from importlib.metadata import version
    model = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                  if line.startswith("model name")), "unknown processor")
    print(f'Reranker try-out on the kept test of "{data["collection"]}": {len(data["items"])} questions, {passages:,} passages '
          f"to score for each model and each way of reading. Nothing in HunterAI or in the collection is changed.")
    print(f"{model}; {threads} threads" + (f" on its {len(cpus)} fast cores" if cpus else "") + f"; onnxruntime {version('onnxruntime')}.")
    listed = ", ".join(f"{m} ({MODELS[m]['mb']} MB, {MODELS[m]['speaks']})" for m in ASKED)
    print(f"Models: {listed}. They are downloaded once into {CACHE}.")
    wide = 44
    cells = lambda c: "".join(f"{c.get(k, (0, 0))[0]:>21}{c.get(k, (0, 0))[1]:>7}" for k in kinds)
    head = "".join(f"{names.get(k, k) + ' (' + str(asked[k]) + ')':>28}" for k in kinds)
    print(f"\n  {'':<{wide}}{head}{'seconds for a question':>28}")
    print(f"  {'':<{wide}}" + "".join(f"{'first':>21}{'msg':>7}" for _ in kinds) + f"{'half took less / longest':>28}")
    before = judged(data, [item["now"] for item in data["items"]])
    base = count(data, before)
    print(f"  {'as the search orders them now':<{wide}}{cells(base)}")
    ctx = multiprocessing.get_context("fork")
    for name in ASKED:
        queue = ctx.Queue()
        child = ctx.Process(target=one_model, args=(name, data, threads, cpus, queue))
        child.start()
        result = None
        while result is None:
            try:
                result = queue.get(timeout=1)
            except Empty:
                if not child.is_alive() and queue.empty():
                    result = {"name": name, "error": f"it ended with code {child.exitcode} (out of memory?)"}
        child.join()
        m = MODELS[name]
        if result.get("error"):
            print(f"\n  {name}: it could not be tried ({result['error']})", flush=True)
            continue
        print(f"\n  {name} ({m['mb']} MB, {m['speaks']}; it is given {m['max_len']} tokens of a question and its passage at most; "
              f"loaded in {result['loaded']:.1f} s, held {result['ram']:,.0f} MB of memory)")
        for form, said in FORMS:
            f = result["forms"][form]
            print(f"    {said} ({f['cut']:,} of {f['pairs']:,} passages were longer than that: their end was not read)")
            for which, what in (("first", f"the first {FIRST} reordered"), ("kept", f"the {data['pool']} kept reordered"),
                                ("all", "all the halves bring, reordered")):
                half, longest = f["seconds"][which]
                print(f"  {'      ' + what:<{wide}}{cells(f['counts'][which])}{half:>19.1f} / {longest:.1f}", flush=True)
            came = [i for i, (was, now) in enumerate(zip(before, f["kept"])) if now[1] and not was[1]]
            fell = [i for i, (was, now) in enumerate(zip(before, f["kept"])) if was[1] and not now[1]]
            short = lambda i: " ".join(data["items"][i]["query"].split())[:70]
            print(f"        with the {data['pool']} kept reordered, {len(came)} came into the message and {len(fell)} fell out of it"
                  + (": " + "; ".join(f'"{short(i)}"' for i in fell) if fell else "") + ".", flush=True)
    print(f"\nfirst: the passage that answers the question stood first. msg: it was in the message ({data['top']} passages at most, "
          f"{data['room']:,} characters).\nThe models stay in {CACHE}: delete that folder when you have no more use for them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
