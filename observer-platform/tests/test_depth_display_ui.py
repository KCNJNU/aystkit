"""Static contract for the complete depth-distance display."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_preview_depth_legend_is_display_only_and_bounded():
    source = (ROOT / 'deploy/jetson/berxel_preview/camera_codec.py').read_text()
    assert 'append_depth_scale' in source
    assert "'0', '1', '2', '3', '4', '>=5m'" in source
    assert 'Display scale, not sensor range' in source
    assert 'black: invalid' in source
    # The legend is appended only to the encoded preview; raw ROS bytes stay untouched.
    assert 'image = append_depth_scale(image)' in source
    assert 'bytes(message.data)' not in source


def test_observer_explains_center_depth_and_pointcloud_measurement_scope():
    source = (ROOT / 'cloud/frontend/media.js').read_text()
    for text in ('中心 5×5 邻域稳健值', '颜色：0–5 m', '采样最近', 'Z 色阶（米）', '网格 1 格约'):
        assert text in source
    assert 'pointsUnchanged' in source
    assert 'validCloudPoints' in source


def test_frontend_javascript_parses():
    result = subprocess.run(['node', '--check', str(ROOT / 'cloud/frontend/media.js')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
