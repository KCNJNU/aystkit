"""Read-only network rates must distinguish zero traffic from missing counters."""
from collector.legacy.system_metrics import NetworkRateSampler


def test_network_rates_handle_warmup_zero_reset_disappearance_and_long_gap():
    now = [0.0]
    sampler = NetworkRateSampler(lambda: now[0])

    def sample(rx=100, tx=200, up=True):
        return sampler.apply([{'name': 'eth0', 'up': up, 'rxBytes': rx, 'txBytes': tx}])[0]

    assert sample()['rxBytesPerSec'] is None
    now[0] = 2
    row = sample(2148, 1224)
    assert row['rxBytesPerSec'] == 1024 and row['txBytesPerSec'] == 512
    now[0] = 3
    assert sample(2148, 1224)['rxBytesPerSec'] == 0
    now[0] = 4
    assert sample(0, 0)['rxBytesPerSec'] is None
    now[0] = 5
    assert sample(None, None)['rxBytesPerSec'] is None
    now[0] = 6
    assert sample(3000, 2000)['rxBytesPerSec'] is None
    now[0] = 7
    assert sample(4000, 3000, False)['rxBytesPerSec'] is None
    now[0] = 8
    assert sample(5000, 4000)['rxBytesPerSec'] is None
    now[0] = 20
    assert sample(6000, 5000)['rxBytesPerSec'] is None
    now[0] = 21
    assert sample(7024, 5000)['rxBytesPerSec'] == 1024
