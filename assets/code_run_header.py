import sys, os, json, re, time, subprocess
# The header is inlined into a temporary script that may live in any cwd, so
# dirname(__file__) cannot locate the repository. code_run passes the project
# root explicitly; fall back to the historical layout for direct invocation.
_code_run_root = os.environ.get('GA_CODE_RUN_ROOT')
if _code_run_root and os.path.isdir(_code_run_root) and _code_run_root not in sys.path:
    sys.path.insert(0, _code_run_root)
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'memory'))
_r = subprocess.run
def _d(b):
    if not b: return ''
    if isinstance(b, str): return b
    try: return b.decode()
    except: return b.decode('gbk', 'replace')
def _run(*a, **k):
    t = k.pop('text', 0) | k.pop('universal_newlines', 0)
    enc = k.pop('encoding', None)
    k.pop('errors', None)
    if enc: t = 1
    if t and isinstance(k.get('input'), str):
        k['input'] = k['input'].encode()
    r = _r(*a, **k)
    if t:
        if r.stdout is not None: r.stdout = _d(r.stdout)
        if r.stderr is not None: r.stderr = _d(r.stderr)
    return r
subprocess.run = _run
_Pi = subprocess.Popen.__init__
def _pinit(self, *a, **k):
    if os.name == 'nt': k['creationflags'] = (k.get('creationflags') or 0) | 0x08000000
    _Pi(self, *a, **k)
subprocess.Popen.__init__ = _pinit
if os.environ.get('GA_WORKFLOW_WORKSPACE_ROOT'):
    from workflow_workspace_guard import install as _install_workspace_guard
    _install_workspace_guard(os.environ['GA_WORKFLOW_WORKSPACE_ROOT'])
sys.excepthook = lambda t, v, tb: (sys.__excepthook__(t, v, tb), print(f"\n[Agent Hint]: NO GUESSING! You MUST probe first. If missing common package, pip.")) if issubclass(t, (ImportError, AttributeError)) else sys.__excepthook__(t, v, tb)
