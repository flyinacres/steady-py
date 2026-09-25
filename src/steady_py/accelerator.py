"""GPU and accelerator detection: which frameworks a notebook uses, and whether each one can see a
device in the running environment."""
import importlib
import logging
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from steady_py import util
from steady_py.constants import CANONICAL_TO_FRAMEWORK_DISPLAY, SUPPORTED_GPU_FRAMEWORKS, TRANSITIVE_FRAMEWORK_MAP
from steady_py.models import GpuInfo

logger = logging.getLogger("steady_py.accelerator")


def expand_transitive_frameworks(imports: Any) -> Set[str]:
    """Expands a set or list of import stems to include their base GPU framework."""
    expanded = set(imports)
    for pkg in imports:
        base_fw = TRANSITIVE_FRAMEWORK_MAP.get(pkg)
        if base_fw:
            expanded.add(base_fw)
        else:
            try:
                reqs = importlib.metadata.requires(pkg) or []
                for req in reqs:
                    req_lower = req.lower()
                    for fw in SUPPORTED_GPU_FRAMEWORKS:
                        if fw in req_lower:
                            expanded.add(fw)
            except importlib.metadata.PackageNotFoundError:
                pass  # not installed here, so its requirements can't be read
            except Exception as e:
                logger.debug(f"Could not read the requirements of '{pkg}': {e}", exc_info=True)
    return expanded


class GpuProbeResult(NamedTuple):
    """Result of a single framework's GPU/accelerator probe."""
    accelerator_type: str
    device_name: str


def probe_torch_gpu() -> Optional[GpuProbeResult]:
    """Probes PyTorch for CUDA or Apple Silicon MPS acceleration."""
    try:
        import torch
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"[HardwareProbe] PyTorch import failed: {e}")
        raise

    if torch.cuda.is_available():
        dev_name = f"{torch.cuda.get_device_name(0)} (via PyTorch)"
        return GpuProbeResult("NVIDIA CUDA", dev_name)
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return GpuProbeResult("Apple Silicon MPS", "Apple Silicon GPU (Metal via PyTorch)")
    return None


def probe_tensorflow_gpu() -> Optional[GpuProbeResult]:
    """Probes TensorFlow for GPU acceleration while silencing C++ CUDA driver noise."""
    try:
        with util.silence_fd2_stderr():
            import tensorflow as tf
            gpus = tf.config.list_physical_devices('GPU')
            if not gpus:
                return None
            dev_name = "NVIDIA GPU (via TensorFlow)"
            try:
                details = tf.config.experimental.get_device_details(gpus[0])
                dev_name = f"{details.get('device_name', 'NVIDIA GPU')} (via TensorFlow)"
            except Exception:
                pass  # keep the generic name; cannot log here, stderr (fd 2) is silenced inside this block
            return GpuProbeResult("GPU", dev_name)
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"[HardwareProbe] TensorFlow GPU probe failed unexpectedly: {e}")
        raise


def probe_jax_gpu() -> Optional[GpuProbeResult]:
    """Probes JAX for GPU/TPU acceleration while silencing C++ CUDA driver noise."""
    try:
        with util.silence_fd2_stderr():
            import jax
            accelerators = [d for d in jax.devices() if d.platform.lower() in ("gpu", "tpu", "metal")]
            if not accelerators:
                return None
            first_accel = accelerators[0]
            accel_type = first_accel.platform.upper()
            dev_name = f"{accel_type} ({first_accel.device_kind}) via JAX"
            return GpuProbeResult(accel_type, dev_name)
    except ImportError:
        return None
    except Exception as e:
        logger.debug(f"[HardwareProbe] JAX GPU probe failed unexpectedly: {e}")
        raise


GPU_PROBES: List[Tuple[str, Callable[[], Optional[GpuProbeResult]]]] = [
    ("torch", probe_torch_gpu),
    ("tensorflow", probe_tensorflow_gpu),
    ("jax", probe_jax_gpu),
]


def inspect_gpu_environment(imported_packages: Any) -> Optional[GpuInfo]:
    """Coordinates per-framework GPU/accelerator probing across PyTorch, TensorFlow, and JAX."""
    expanded_imports = expand_transitive_frameworks(imported_packages)
    found_frameworks = list(SUPPORTED_GPU_FRAMEWORKS.intersection(expanded_imports))
    if not found_frameworks:
        return None

    framework_devices: Dict[str, Optional[str]] = {}
    active_types: List[str] = []
    probe_errors: List[str] = []
    primary_fw: Optional[str] = None
    primary_dev: Optional[str] = None

    for fw_stem, probe in GPU_PROBES:
        if fw_stem not in found_frameworks:
            continue
        try:
            result = probe()
            if result:
                framework_devices[fw_stem] = result.device_name
                active_types.append(result.accelerator_type)
                if not primary_dev:
                    primary_fw = CANONICAL_TO_FRAMEWORK_DISPLAY.get(fw_stem, fw_stem.capitalize())
                    primary_dev = result.device_name
            else:
                framework_devices[fw_stem] = None
        except Exception as e:
            framework_devices[fw_stem] = None
            fw_label = CANONICAL_TO_FRAMEWORK_DISPLAY.get(fw_stem, fw_stem.capitalize())
            probe_errors.append(f"{fw_label} probe error: {e}")

    has_gpu = primary_dev is not None

    return GpuInfo(
        has_gpu=has_gpu,
        type=active_types[0] if active_types else None,
        active_framework=primary_fw,
        device_name=primary_dev,
        frameworks=sorted(found_frameworks),
        framework_devices=framework_devices,
        probe_errors=probe_errors
    )


def resolve_notebook_gpu_info(nb_imports: Any, batch_hw_cache: Optional[GpuInfo]) -> Optional[GpuInfo]:
    """Matches a notebook's specific imports against the active batch hardware cache."""
    if not batch_hw_cache:
        return None

    expanded_nb_imports = expand_transitive_frameworks(nb_imports)
    nb_fw = set(batch_hw_cache.frameworks).intersection(expanded_nb_imports)
    if not nb_fw:
        return None

    fw_devices = batch_hw_cache.framework_devices
    matched_fw = None
    matched_device = None

    for fw_stem in sorted(nb_fw):
        if fw_devices.get(fw_stem):
            matched_fw = fw_stem
            matched_device = fw_devices[fw_stem]
            break

    if matched_device and matched_fw:
        active_label = CANONICAL_TO_FRAMEWORK_DISPLAY.get(matched_fw, matched_fw.capitalize())
        return GpuInfo(
            has_gpu=True,
            type=batch_hw_cache.type,
            active_framework=active_label,
            device_name=matched_device,
            frameworks=sorted(nb_fw),
            framework_devices=fw_devices
        )
    else:
        return GpuInfo(
            has_gpu=False,
            type=None,
            active_framework=None,
            device_name=None,
            frameworks=sorted(nb_fw),
            framework_devices=fw_devices
        )

