"""Running independent pieces of a build at the same time.

texconv compresses on one core (a 4096 texture: 16.1 s of CPU in 14.9 s of
wall time) and four copies at once finish in 16 s rather than 60, so the
texture steps hand their independent conversions to a small pool. The
Blender stage is split into separate Blender processes the same way (see
build_track.blender_stage).

--jobs 1 anywhere runs everything one at a time, exactly as before.
"""
import os
from concurrent.futures import ThreadPoolExecutor


# Memory one job may need. Measured on Laguna with six jobs: each Blender
# peaked at 1.6 GB, and the texture step at 2.15 GB for all six together.
# Spa's main model is a fifth bigger than Laguna's, so this leaves room.
GB_PER_JOB = 2.5


def free_memory_gb():
    """Physical memory available right now, or None where we cannot ask."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return m.ullAvailPhys / 1024.0 ** 3
    except Exception:
        return None


def default_jobs():
    """Half the logical CPUs, at most 6, and no more than free memory allows.

    Before Blender's geometry was decoded on demand (kn5._lazy), six
    Blenders took Laguna to 0.2 GB of free memory on a 32 GB machine. The
    memory cap keeps a smaller machine from paging instead of working.
    """
    jobs = max(1, min(6, (os.cpu_count() or 2) // 2))
    free = free_memory_gb()
    if free is not None:
        jobs = max(1, min(jobs, int(free / GB_PER_JOB)))
    return jobs


def ordered_map(fn, items, jobs, tmp):
    """fn(item, tmp_dir) over items, results in the order of items.

    Each task gets a scratch folder of its own, because the texture helpers
    name their temporary files after the source texture and two copies of one
    texture (Laguna's trees at alpha 51 and 77) would otherwise trample each
    other. With jobs 1 every task runs here, in order, in tmp itself.
    """
    items = list(items)
    if jobs <= 1 or len(items) <= 1:
        return [fn(it, tmp) for it in items]

    def one(i_it):
        i, it = i_it
        sub = os.path.join(tmp, "j%d" % i)
        os.makedirs(sub, exist_ok=True)
        return fn(it, sub)

    with ThreadPoolExecutor(max_workers=jobs) as ex:
        return list(ex.map(one, enumerate(items)))
