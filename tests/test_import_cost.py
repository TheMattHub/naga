"""Import cost (D915): `naga --help` skips the plugins, `import nagasim.api` skips openpyxl,
the package exposes its three main names lazily. Each check runs in a fresh interpreter."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)


def _py(code: str) -> subprocess.CompletedProcess:
    # No -I: it drops PYTHONPATH, and the child would import the editable install, not this tree.
    env = {**os.environ, "PYTHONPATH": ROOT}
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=env, timeout=120)


def test_help_skips_plugins():
    r = _py("import sys, nagasim.cli as c\n"
            "rc = c.main(['--help'])\n"
            "print('PLUGINS', 'nagasim.plugins' in sys.modules, rc)")
    assert "PLUGINS False 0" in r.stdout, r.stderr
    assert "commands:" in r.stdout and "  run " in r.stdout


def test_bare_naga_skips_plugins():
    r = _py("import sys, nagasim.cli as c\n"
            "rc = c.main([])\n"
            "print('PLUGINS', 'nagasim.plugins' in sys.modules, rc)")
    assert "PLUGINS False 0" in r.stdout, r.stderr


def test_api_import_skips_openpyxl():
    r = _py("import sys, nagasim.api\nprint('OPENPYXL', 'openpyxl' in sys.modules)")
    assert "OPENPYXL False" in r.stdout, r.stderr


def test_top_level_names():
    r = _py("import sys, nagasim\n"
            "assert 'nagasim.api' not in sys.modules\n"
            "import nagasim.api, nagasim.build, nagasim.results\n"
            "assert nagasim.run_model is nagasim.api.run_model\n"
            "assert nagasim.Model is nagasim.build.Model\n"
            "assert nagasim.Results is nagasim.results.Results\n"
            "assert {'run_model', 'Model', 'Results'} <= set(dir(nagasim))\n"
            "assert isinstance(nagasim.__version__, str)\n"
            "try:\n    nagasim.nope\nexcept AttributeError:\n    print('OK')")
    assert "OK" in r.stdout, r.stderr


def test_excel_still_writes(simple_model_path, tmp_path):
    import openpyxl
    from nagasim import api
    out = tmp_path / "r.xlsx"
    api.run_model(simple_model_path, replications=1, run_length=20).to_excel(out)
    assert openpyxl.load_workbook(out).sheetnames
