from procular.providers.system import cpu_caches
from procular.util.timeline import SampleTime


def _fake_cache(root, cpu, index, level, ctype, size, shared):
    d = root / f"cpu{cpu}" / "cache" / f"index{index}"
    d.mkdir(parents=True)
    for name, value in (("level", level), ("type", ctype), ("size", size), ("shared_cpu_list", shared)):
        (d / name).write_text(f"{value}\n")


def test_cpu_caches_counts_shared_instances_once(tmp_path):
    # hybrid CPU: cpu0-1 are a P-core (SMT) with private L1/L2, cpu2-3 are E-cores sharing an L2
    for cpu in range(4):
        p = cpu < 2
        core = "0-1" if p else str(cpu)
        _fake_cache(tmp_path, cpu, 0, 1, "Data", "48K" if p else "32K", core)
        _fake_cache(tmp_path, cpu, 1, 1, "Instruction", "32K" if p else "64K", core)
        _fake_cache(tmp_path, cpu, 2, 2, "Unified", "2048K", "0-1" if p else "2-3")
        _fake_cache(tmp_path, cpu, 3, 3, "Unified", "12M", "0-3")
    (tmp_path / "cpufreq").mkdir()  # not a cpuN dir: ignored
    assert cpu_caches(str(tmp_path)) == [
        ("L1d", [48 * 1024, 32 * 1024, 32 * 1024]),
        ("L1i", [64 * 1024, 64 * 1024, 32 * 1024]),
        ("L2", [2 << 20, 2 << 20]),
        ("L3", [12 << 20]),
    ]


def test_cpu_caches_missing_sysfs(tmp_path):
    assert cpu_caches(str(tmp_path / "nope")) == []


def _snap(t, disks=(), nics=(), wifi=()):
    return {"time": SampleTime(t, 1000 + t), "cpu_total": 0, "cpu_kernel": 0, "cpu_cores": [],
            "mem_used": 0, "mem_cache": 0,
            "swap_used": 0, "io_read": 0, "io_write": 0, "disk_read": 0, "disk_write": 0, "net_rx": 0, "net_tx": 0,
            "disks": {d: {"read_rate": 5, "write_rate": 6, "busy": 7} for d in disks},
            "nics": {n: {"rx_rate": 8, "tx_rate": 9} for n in nics}, "wifi_quality": {w: 50 for w in wifi}}


def test_history_marks_missing_time_but_not_an_interval_change():
    from procular.providers.system import History

    h = History()
    for elapsed, interval in [(1, 1), (2, 1), (7, 5), (12, 5), (17, 1), (18, 1), (45, 1)]:
        h.push(_snap(elapsed), None, None, None, interval=interval)
    assert [t.elapsed for t in h.times] == [1, 2, 7, 12, 17, 18, 45]
    assert [t.gap for t in h.times] == [False] * 6 + [True]


def test_history_of_a_device_stays_aligned_while_it_is_away():
    from procular.providers.system import History
    h = History(size=100)
    for t in range(30):
        present = t < 10 or t >= 20
        h.push(_snap(t, disks=["sda"] + (["sdb"] if present else []), nics=["eth0"] + (["tun0"] if present else []),
                     wifi=["wlan0"] if present else []), None, None, None)
    assert len(h.times) == 30
    for queue in (*h.per_disk["sdb"], h.per_disk_busy["sdb"], *h.per_nic["tun0"], h.wifi_signal["wlan0"]):
        assert len(queue) == 30
        assert not any(list(queue)[10:20]) and all(list(queue)[:10]) and all(list(queue)[20:])
    assert list(h.per_disk["sda"][0]) == [5] * 30
    # a device that shows up later starts aligned too
    h.push(_snap(30, disks=["sda", "sdc"]), None, None, None)
    assert list(h.per_disk["sdc"][0]) == [0] * 30 + [5]


def test_history_drops_devices_that_never_come_back():
    from procular.providers.system import History
    h = History(size=20)
    for t in range(200):  # an interface per container, each with a name of its own
        h.push(_snap(t, disks=["sda"], nics=["eth0", f"veth{t // 5}"]), None, None, None)
    assert "eth0" in h.per_nic and "veth39" in h.per_nic
    assert len(h.per_nic) <= 2 + 20 // 5 + 1
    assert "veth0" not in h.per_nic
    assert not any(name == "veth0" for _table, name in h._missed)
    # back after its history was dropped: it starts over, aligned
    h.push(_snap(200, disks=["sda"], nics=["eth0", "veth0"]), None, None, None)
    assert len(h.per_nic["veth0"][0]) == len(h.times) == 20


def test_read_pressure_and_cgroup_pressure(tmp_path):
    from procular.providers.system import read_cgroup_pressure, read_pressure

    psi = tmp_path / "pressure"
    psi.mkdir()
    (psi / "memory").write_text("some avg10=2.00 avg60=1.00 avg300=0.50 total=1000\n"
                                "full avg10=1.00 avg60=0.50 avg300=0.25 total=400\n")
    (psi / "io").write_text("garbage\n")
    result = read_pressure(str(psi))
    assert set(result) == {"memory"}  # no cpu file, nothing usable in io
    assert result["memory"]["full"]["total"] == 400
    assert read_pressure(str(tmp_path / "missing")) == {}

    unit = tmp_path / "system.slice" / "a.service"
    unit.mkdir(parents=True)
    (unit / "cpu.pressure").write_text("some avg10=3.00 avg60=0.00 avg300=0.00 total=9\n")
    assert read_cgroup_pressure("/system.slice/a.service", str(tmp_path))["cpu"]["some"]["avg10"] == 3.0
    assert read_cgroup_pressure("/system.slice/../../etc", str(tmp_path)) == {}
    assert read_cgroup_pressure("", str(tmp_path)) == {}


def test_pressure_rates_from_counters():
    from procular.providers.system import PressureRates

    def reading(some, full, avg10=7.0):
        return {"memory": {"some": {"avg10": avg10, "total": some}, "full": {"avg10": 0.0, "total": full}},
                "cpu": {"some": {"avg10": 0.0, "total": 0}}}

    rates = PressureRates()
    # nothing to compare with yet: the kernel's 10 second average stands in
    assert rates.update(reading(1_000_000, 0), 1.0)["memory_some"] == 7.0
    # 250 ms of stall in a 2 second interval: 12.5%
    second = rates.update(reading(1_250_000, 100_000), 2.0)
    assert second["memory_some"] == 12.5
    assert second["memory_full"] == 5.0
    assert second["cpu_some"] == 0.0
    assert "io_some" not in second  # not reported
    # a counter that went backwards (a new cgroup of the same name) falls back to avg10
    assert rates.update(reading(10, 0, avg10=1.5), 1.0)["memory_some"] == 1.5
    assert rates.update({}, 1.0) is None


def test_history_records_pressure():
    from procular.providers.system import History

    h = History(10)
    h.push({**_snap(1), "pressure": {"memory_some": 4.0}}, None, None, None, interval=1.0)
    h.push({**_snap(2), "pressure": None}, None, None, None, interval=1.0)  # without PSI
    assert list(h.pressure["memory_some"]) == [4.0, 0.0]
    assert list(h.pressure["io_full"]) == [0.0, 0.0]
