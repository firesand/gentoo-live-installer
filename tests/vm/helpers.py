import importlib.util
import sys
from pathlib import Path


def load_vm_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "test-vm.py"
    spec = importlib.util.spec_from_file_location("test_vm_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB6l6y6nQ6r8Q6w8Q6w8Q6w8Q6w8Q6w8Q6w8Q6w8Q6w8Q6 test@vm"
