"""极简 pytest 垫片：仅供本环境无 pytest 时跑 tests/ 用。

支持现有测试实际用到的能力：``pytest.raises``、无参/``tmp_path``/``clock``
夹具（从 conftest.py 收集）、``testpaths`` 目录收集与 -q 风格汇总。
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import tempfile
import traceback
from types import ModuleType

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class raises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"期望抛出 {self.exc_type.__name__}，但未抛出")
        if not issubclass(exc_type, self.exc_type):
            raise exc
        self.value = exc
        return True


def fixture(func=None):
    if func is None:
        return lambda f: _mark_fixture(f)
    return _mark_fixture(func)


def _mark_fixture(func):
    func._pytest_fixture = True
    return func


def mark():  # pragma: no cover - 占位
    def deco(obj):
        return obj

    deco.parametrize = lambda *a, **k: (lambda obj: obj)
    return deco


class _Approx(float):
    def __new__(cls, expected, rel=None, abs=None):  # noqa: A002
        obj = super().__new__(cls, expected)
        obj.expected = float(expected)
        obj.rel = 1e-6 if rel is None else rel
        obj.abs = 1e-12 if abs is None else abs
        return obj

    def __eq__(self, other):
        try:
            other = float(other)
        except (TypeError, ValueError):
            return NotImplemented
        return abs(other - self.expected) <= max(self.abs, self.rel * abs(self.expected))


class _Capture:
    """capsys 垫片：进入时全局重定向 stdout/stderr，读取时取出累计内容。"""

    def __init__(self):
        import io

        self._io = io
        self._buf_out = io.StringIO()
        self._buf_err = io.StringIO()
        self._buf_out.reconfigure = lambda **kw: None  # type: ignore[attr-defined]
        self._buf_err.reconfigure = lambda **kw: None  # type: ignore[attr-defined]
        self._old_out = sys.stdout
        self._old_err = sys.stderr
        sys.stdout = self._buf_out
        sys.stderr = self._buf_err

    def readouterr(self):
        class _R:
            pass

        r = _R()
        r.out = self._buf_out.getvalue()
        r.err = self._buf_err.getvalue()
        self._buf_out.seek(0)
        self._buf_out.truncate(0)
        self._buf_err.seek(0)
        self._buf_err.truncate(0)
        return r

    def restore(self):
        sys.stdout = self._old_out
        sys.stderr = self._old_err


pytest_shim = ModuleType("pytest")
pytest_shim.raises = raises
pytest_shim.fixture = fixture
pytest_shim.mark = mark()
pytest_shim.skip = lambda *a, **k: None
pytest_shim.approx = lambda expected, rel=None, abs=None: _Approx(expected, rel, abs)
sys.modules["pytest"] = pytest_shim


def _load_module(name: str, path: pathlib.Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _collect_fixtures(module: ModuleType) -> dict:
    fixtures = {}
    for name, obj in vars(module).items():
        if callable(obj) and getattr(obj, "_pytest_fixture", False):
            fixtures[name] = obj
    return fixtures


def main() -> int:
    conftest = _load_module("conftest", ROOT / "tests" / "conftest.py")
    base_fixtures = _collect_fixtures(conftest)

    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    passed = 0
    failures: list[tuple[str, object]] = []

    for test_file in test_files:
        module = _load_module(f"tests.{test_file.stem}", test_file)
        local_fixtures = _collect_fixtures(module)
        funcs = [
            (name, obj)
            for name, obj in vars(module).items()
            if name.startswith("test_") and callable(obj)
        ]

        def resolve(maker, stack, tmp_path):
            fparams = inspect.signature(maker).parameters
            fkwargs = {}
            for pname in fparams:
                if pname in stack:
                    raise RuntimeError(f"夹具循环依赖: {stack + [pname]}")
                if pname in local_fixtures:
                    fkwargs[pname] = resolve(local_fixtures[pname], stack + [pname], tmp_path)
                elif pname in base_fixtures:
                    fkwargs[pname] = resolve(base_fixtures[pname], stack + [pname], tmp_path)
                elif pname == "tmp_path":
                    fkwargs[pname] = tmp_path
                else:
                    raise RuntimeError(f"未知夹具 {pname}")
            return maker(**fkwargs)

        for name, func in funcs:
            params = inspect.signature(func).parameters
            kwargs = {}
            capture = None
            td = tempfile.TemporaryDirectory()
            tmp_path = pathlib.Path(td.name)
            try:
                for pname in params:
                    if pname == "capsys":
                        capture = _Capture()
                        kwargs[pname] = capture
                    elif pname in local_fixtures:
                        kwargs[pname] = resolve(local_fixtures[pname], [pname], tmp_path)
                    elif pname in base_fixtures:
                        kwargs[pname] = resolve(base_fixtures[pname], [pname], tmp_path)
                    elif pname == "tmp_path":
                        kwargs[pname] = tmp_path
                    else:
                        raise RuntimeError(f"未知夹具 {pname} for {test_file.name}::{name}")
                func(**kwargs)
                passed += 1
            except Exception as exc:  # noqa: BLE001
                failures.append((f"{test_file.name}::{name}", exc))
                traceback.print_exc()
            finally:
                if capture is not None:
                    capture.restore()
                td.cleanup()

    print(f"\n{passed} passed, {len(failures)} failed")
    for name, exc in failures:
        print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
