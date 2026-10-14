import tk8s


def test_package_imports() -> None:
    assert tk8s.__doc__ is not None and "Quadlet" in tk8s.__doc__
