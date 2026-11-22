import time

from hunterai import thermal
from hunterai.thermal import Part

CPU = {"name": "processor", "limit": 90.0, "setting": "HUNTERAI_MAX_CPU_TEMP"}
GPU = {"name": "graphics card", "limit": 85.0, "setting": "HUNTERAI_MAX_GPU_TEMP"}
ASKED = '#!/bin/sh\necho "$*" >> "$FAKE_OUT/asked"\n'


def sensor(root, n, name, milli):
    d = root / f"hwmon{n}"; d.mkdir()
    (d / "name").write_text(name + "\n")
    if milli is not None:
        (d / "temp1_input").write_text(f"{milli}\n")


def card(monkeypatch, *readings):
    """The graphics card says these, one per look; the last one from then on."""
    left = list(readings)

    async def read():
        return left.pop(0) if len(left) > 1 else left[0]
    monkeypatch.setattr(thermal, "gpu_temp", read)


def default_limits(monkeypatch):
    for name in ("HUNTERAI_MAX_CPU_TEMP", "HUNTERAI_MAX_GPU_TEMP"):
        monkeypatch.delenv(name, raising=False)


def test_the_processor_temperature_is_read_from_its_sensor(tmp_path, monkeypatch):
    monkeypatch.setattr(thermal, "HWMON", tmp_path)
    assert thermal.cpu_temp() is None
    sensor(tmp_path, 0, "nvme", 41000); sensor(tmp_path, 1, "acpi_fan", None); sensor(tmp_path, 2, "coretemp", 97000)
    assert thermal.cpu_temp() == 97.0
    monkeypatch.setattr(thermal, "HWMON", tmp_path / "missing")
    assert thermal.cpu_temp() is None


def test_the_limit_can_be_set_or_turned_off(monkeypatch):
    monkeypatch.delenv("HUNTERAI_MAX_CPU_TEMP", raising=False)
    assert thermal.limit() == thermal.DEFAULT_LIMIT
    for raw, want in (("85", 85.0), ("off", None), ("0", None), ("hot", thermal.DEFAULT_LIMIT)):
        monkeypatch.setenv("HUNTERAI_MAX_CPU_TEMP", raw)
        assert thermal.limit() == want


def test_the_graphics_card_has_a_limit_of_its_own(monkeypatch):
    default_limits(monkeypatch)
    assert thermal.gpu_limit() == thermal.GPU_DEFAULT_LIMIT == 85.0
    monkeypatch.setenv("HUNTERAI_MAX_CPU_TEMP", "70")                      # the processor's limit is not the card's
    assert thermal.gpu_limit() == 85.0
    for raw, want in (("80", 80.0), ("off", None), ("0", None), ("hot", 85.0)):
        monkeypatch.setenv("HUNTERAI_MAX_GPU_TEMP", raw)
        assert thermal.gpu_limit() == want
    assert thermal.limit() == 70.0


async def test_the_graphics_card_temperature_is_what_nvidia_smi_says(fake_bin, monkeypatch):
    monkeypatch.setattr(thermal, "GPU_KEEP", 0); monkeypatch.setattr(thermal, "GPU_RETRY", 0)      # every look asks anew
    assert await thermal.gpu_temp() is None                                # a machine without the program
    monkeypatch.setattr(thermal, "NVIDIA_SMI", "nvidia-smi")
    out = fake_bin("nvidia-smi", ASKED + "echo 71\n")
    assert await thermal.gpu_temp() == 71.0
    assert (out / "asked").read_text().split() == ["--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]
    fake_bin("nvidia-smi", "#!/bin/sh\necho 64\necho '[N/A]'\necho 83\n")    # two cards and one that does not say: the hottest
    assert await thermal.gpu_temp() == 83.0
    for broken in ("#!/bin/sh\necho 'NVIDIA-SMI has failed because it could not communicate with the NVIDIA driver.'\nexit 9\n",
                   "#!/bin/sh\necho\n", "#!/bin/sh\necho 71\nexit 1\n"):
        fake_bin("nvidia-smi", broken)
        assert await thermal.gpu_temp() is None, broken


async def test_a_nvidia_smi_that_hangs_is_not_waited_for(fake_bin, monkeypatch):
    monkeypatch.setattr(thermal, "NVIDIA_SMI", "nvidia-smi")
    monkeypatch.setattr(thermal, "GPU_TIMEOUT", 0.2)
    fake_bin("nvidia-smi", "#!/bin/sh\nexec sleep 30\n")
    began = time.monotonic()
    assert await thermal.gpu_temp() is None and time.monotonic() - began < 10


async def test_one_look_at_the_graphics_card_serves_for_a_moment(fake_bin, monkeypatch):
    monkeypatch.setattr(thermal, "NVIDIA_SMI", "nvidia-smi")
    out = fake_bin("nvidia-smi", ASKED + "echo 71\n")
    def asked():
        return len((out / "asked").read_text().splitlines())
    assert [await thermal.gpu_temp() for _ in range(5)] == [71.0] * 5 and asked() == 1       # embedding looks before every batch
    monkeypatch.setattr(thermal, "GPU_KEEP", 0)
    assert await thermal.gpu_temp() == 71.0 and asked() == 2
    fake_bin("nvidia-smi", ASKED + "exit 9\n")                             # a program that fails is not started for every batch
    assert [await thermal.gpu_temp() for _ in range(3)] == [None] * 3 and asked() == 3
    monkeypatch.setattr(thermal, "GPU_RETRY", 0)
    assert await thermal.gpu_temp() is None and asked() == 4


async def test_a_part_at_its_limit_is_hot(monkeypatch):
    default_limits(monkeypatch)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 89.0); card(monkeypatch, 84.0)
    assert await thermal.hot() is None
    card(monkeypatch, 85.0)
    assert await thermal.hot() == Part(degrees=85.0, **GPU)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 93.0)
    assert await thermal.hot() == Part(degrees=93.0, **CPU)                 # the processor is named first
    monkeypatch.setenv("HUNTERAI_MAX_CPU_TEMP", "off")
    assert await thermal.hot() == Part(degrees=85.0, **GPU)
    monkeypatch.setenv("HUNTERAI_MAX_GPU_TEMP", "off")
    assert await thermal.hot() is None
    default_limits(monkeypatch)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: None); card(monkeypatch, None)       # nothing can be read: nothing is held up
    assert await thermal.hot() is None


async def test_a_hot_processor_is_waited_for_and_then_given_up_on(monkeypatch):
    default_limits(monkeypatch)
    readings = iter([97.0, 95.0, 84.0])
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings))
    assert await thermal.wait_cool(wait=5, step=0.001) is None
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 99.0)
    assert await thermal.wait_cool(wait=0.01, step=0.001) == Part(degrees=99.0, **CPU)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: None)
    assert await thermal.wait_cool(wait=0.01, step=0.001) is None
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 99.0)
    monkeypatch.setenv("HUNTERAI_MAX_CPU_TEMP", "off")
    assert await thermal.wait_cool(wait=0.01, step=0.001) is None


async def test_a_hot_graphics_card_is_waited_for_like_the_processor(monkeypatch):
    default_limits(monkeypatch)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 60.0); card(monkeypatch, 88.0, 86.0, 85.0, 84.0)
    told = []

    async def tell(part):
        told.append(part)
    assert await thermal.wait_cool(wait=5, step=0, tell=tell) is None
    assert told == [Part(degrees=d, **GPU) for d in (88.0, 86.0, 85.0)]     # at the limit is still hot
    card(monkeypatch, 91.0)
    assert await thermal.wait_cool(wait=0.01, step=0.001) == Part(degrees=91.0, **GPU)


async def test_work_that_waited_goes_on_a_few_degrees_under_the_limit(monkeypatch):
    default_limits(monkeypatch)
    readings, told = iter([97.0, 95.0, 88.0, 85.0]), []
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings))

    async def tell(part):
        told.append(part.degrees)
    assert await thermal.wait_cool(wait=5, step=0, margin=5, tell=tell) is None
    assert told == [97.0, 95.0, 88.0]                                       # 88 is under the limit of 90, and not enough
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 86.0)                  # never at its limit: the margin is for what was hot
    assert await thermal.wait_cool(wait=5, step=0, margin=5) is None
    readings, told = iter([90.0, 88.0, 85.0]), []                           # exactly at the limit starts a wait as well
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings))
    assert await thermal.wait_cool(wait=5, step=0, margin=5, tell=tell) is None and told == [90.0, 88.0]


async def test_only_a_part_that_was_hot_is_waited_for(monkeypatch):
    default_limits(monkeypatch)
    readings = iter([91.0, 84.0])
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings)); card(monkeypatch, 82.0)     # the card: warm, never at 85
    assert await thermal.wait_cool(wait=5, step=0, margin=5) is None


async def test_a_part_that_gets_hot_during_the_wait_is_waited_for_too(monkeypatch):
    default_limits(monkeypatch)
    readings, told = iter([91.0, 84.0, 84.0, 84.0]), []
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings)); card(monkeypatch, 80.0, 86.0, 83.0, 79.0)

    async def tell(part):
        told.append((part.name, part.degrees))
    assert await thermal.wait_cool(wait=5, step=0, margin=5, tell=tell) is None
    assert told == [("processor", 91.0), ("graphics card", 86.0), ("graphics card", 83.0)]
