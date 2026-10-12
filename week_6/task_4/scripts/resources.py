"""Sample resources without changing Ollama or the machine configuration."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
from threading import Event, Thread
import time

import httpx


def own_rss():
    if os.name != 'nt':
        return None
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    api = ctypes.WinDLL('psapi', use_last_error=True)
    api.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    counter = Counters()
    counter.cb = ctypes.sizeof(counter)
    return counter.WorkingSetSize if api.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counter), counter.cb) else None


def runtime_metadata():
    result = {'errors': []}
    with httpx.Client(base_url='http://127.0.0.1:11434', timeout=5, trust_env=False,
                      follow_redirects=False) as client:
        for key in ('version', 'tags', 'ps'):
            try:
                result[key] = client.get('/api/' + key).raise_for_status().json()
            except (httpx.HTTPError, ValueError) as error:
                result[key] = None
                result['errors'].append({'stage': key, 'error': type(error).__name__})
    return result


def unload_models(models):
    """Explicit runtime lifecycle operation: empty messages, no inference."""
    events = []
    with httpx.Client(base_url='http://127.0.0.1:11434', timeout=30, trust_env=False,
                      follow_redirects=False) as client:
        for model in dict.fromkeys(models):
            response = client.post('/api/chat', json={'model': model, 'messages': [],
                                                      'stream': False, 'keep_alive': 0})
            response.raise_for_status()
            value = response.json()
            if value.get('done') is not True or value.get('done_reason') != 'unload':
                raise RuntimeError('Ollama did not confirm unload')
            events.append({'model': model, 'operation': 'unload', 'result': value})
    return events


class ResourceSampler:
    """GPU values cover the whole device; samples are not exact peaks."""
    def __init__(self, interval=0.3):
        self.interval = interval
        self.samples = []
        self.errors = []
        self.stop_event = Event()
        self.thread = None

    def __enter__(self):
        self.thread = Thread(target=self._sample, daemon=True)
        self.thread.start()
        return self

    def _sample(self):
        executable = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32' / 'nvidia-smi.exe')
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                run = subprocess.run([executable, '--query-gpu=memory.used,utilization.gpu,power.draw',
                                      '--format=csv,noheader,nounits'], capture_output=True, text=True,
                                     timeout=3, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if run.returncode:
                    raise RuntimeError('nvidia-smi unavailable')
                values = [float(v.strip()) for v in run.stdout.splitlines()[0].split(',')]
                self.samples.append({'time': started, 'gpu_memory_mib': values[0],
                                     'gpu_utilization_percent': values[1], 'gpu_power_watts': values[2]})
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired, RuntimeError) as error:
                self.errors.append(type(error).__name__)
                break
            self.stop_event.wait(max(0.01, self.interval - (time.monotonic() - started)))

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=4)

    def report(self, model):
        resident = None
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                body = client.get('http://127.0.0.1:11434/api/ps').raise_for_status().json()
            resident = next((m for m in body.get('models', []) if m.get('name') == model), None)
        except (httpx.HTTPError, ValueError):
            self.errors.append('runtime_ps_unavailable')
        return {'resident': resident, 'python_rss_bytes': own_rss(),
                'sample_count': len(self.samples), 'sample_interval_seconds': self.interval,
                'sampled_gpu_peak_mib': max((s['gpu_memory_mib'] for s in self.samples), default=None),
                'sampled_gpu_peak_utilization_percent': max((s['gpu_utilization_percent'] for s in self.samples), default=None),
                'sampled_gpu_peak_power_watts': max((s['gpu_power_watts'] for s in self.samples), default=None),
                'samples': self.samples, 'errors': self.errors,
                'scope': 'whole GPU samples; Python process RSS at end; Ollama resident allocation after call'}
