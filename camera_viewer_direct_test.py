"""Start this checkout's Viewer with the experimental r9 neutral transition test B."""
import os
from pathlib import Path
import runpy


def main():
    # Only this process and its children inherit the test settings.
    os.environ['CAMERA_PTZ_CONSERVATIVE_STOPS'] = '0'
    os.environ['CAMERA_PTZ_NEUTRAL_TRANSITIONS'] = '1'
    os.environ['CAMERA_PTZ_TRACE_HTTP'] = '1'
    runpy.run_path(str(Path(__file__).with_name('camera_viewer.py')), run_name='__main__')


if __name__ == '__main__':
    main()
