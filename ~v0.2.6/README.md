# Camera Management System — withdrawn r8 candidate

**Draft, unvalidated: do not use as the recommended version.**

r8 is preserved without code fixes for possible future work. Field testing
revealed that the still-held axis can stop after a partial release and that zoom
can stutter during movement. See [issue #7](https://github.com/THET1TAN/camera-management-system/issues/7)
and the [withdrawal report](../docs/ptz-r8-withdrawal.md).

`main` contains the previous public baseline. r9 is being developed separately
in [PR #4](https://github.com/THET1TAN/camera-management-system/pull/4).
r8 must not be reintroduced without further testing and explicit validation.

Test setup: install Python and the dependencies in `requirements.txt`, then run
`python camera_viewer.py`. Keep the private database and its key together; never
publish either file. The `~v0.2.6` folder preserves the r8 sources with the same
unvalidated status.
