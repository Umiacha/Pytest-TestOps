def pytest_addoption(parser):
    group = parser.getgroup("testops")
    group.addoption(
        "--testops",
        action="store_true",
        default=False,
        help="Enable TestOps report collection.",
    )