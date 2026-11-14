from hunterai import thermal


def sensor(root, n, name, milli):
    d = root / f"hwmon{n}"; d.mkdir()
    (d / "name").write_text(name + "\n")
    if milli is not None:
        (d / "temp1_input").write_text(f"{milli}\n")


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


async def test_a_hot_processor_is_waited_for_and_then_given_up_on(monkeypatch):
    monkeypatch.delenv("HUNTERAI_MAX_CPU_TEMP", raising=False)
    readings = iter([97.0, 95.0, 84.0])
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings))
    assert await thermal.cool_enough(wait=5, step=0.001) == (True, 84.0)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 99.0)
    assert await thermal.cool_enough(wait=0.01, step=0.001) == (False, 99.0)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: None)
    assert await thermal.cool_enough(wait=0.01, step=0.001) == (True, None)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 99.0)
    monkeypatch.setenv("HUNTERAI_MAX_CPU_TEMP", "off")
    assert await thermal.cool_enough(wait=0.01, step=0.001) == (True, 99.0)
