"""
sync_stock.py — Retro 10 / Club 90
Motor de sync bidireccional. Corre cada 5 minutos via Task Scheduler.

Uso:
  python sync_stock.py                  → sync normal
  python sync_stock.py --init           → primer run: setea Club 90 al stock de R10
  python sync_stock.py --dry-run        → simula sin tocar nada (siempre verbose)
  python sync_stock.py --verbose        → muestra detalle de cada variante

Logica de sync:
  - Cada variante mapeada tiene un "pool" (stock fisico real compartido).
  - En --init: pool = stock actual de R10. Se setea Club 90 al mismo valor.
  - En runs normales: se detectan ventas en cada tienda (delta negativo),
    se resta del pool, y ambas tiendas quedan en el nuevo pool.
  - Reposiciones (delta positivo en R10): aumentan el pool y se propagan a Club 90.
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# Rutas relativas al directorio del script (funciona tanto local como en GitHub Actions)
SCRIPT_DIR = Path(__file__).parent
MAP_FILE   = SCRIPT_DIR / "variant_map.json"
STATE_FILE = SCRIPT_DIR / "sync_state.json"
LOG_FILE   = SCRIPT_DIR / "sync_log.txt"

# Tokens desde variables de entorno (GitHub Secrets) o fallback a .env local
def _load_env():
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

STORE_A_ID    = os.environ.get('STORE_A_ID',    '2943107')
STORE_A_TOKEN = os.environ.get('STORE_A_TOKEN', '')
STORE_B_ID    = os.environ.get('STORE_B_ID',    '7428540')
STORE_B_TOKEN = os.environ.get('STORE_B_TOKEN', '')

if not STORE_A_TOKEN or not STORE_B_TOKEN:
    print("ERROR: STORE_A_TOKEN y STORE_B_TOKEN deben estar en variables de entorno o en .env")
    sys.exit(1)

INIT_MODE = '--init'     in sys.argv
DRY_RUN   = '--dry-run'  in sys.argv
VERBOSE   = '--verbose'  in sys.argv or DRY_RUN

API_DELAY = 0.35   # segundos entre llamadas API (evitar rate limit)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg, force=False):
    ts   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    if VERBOSE or force:
        print(line)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


# ── API ───────────────────────────────────────────────────────────────────────

def api_get_variant(store_id, token, product_id, variant_id):
    url  = f"https://api.tiendanube.com/v1/{store_id}/products/{product_id}/variants/{variant_id}"
    req  = urllib.request.Request(
        url, headers={"Authentication": f"bearer {token}", "User-Agent": "RetroClub-Sync/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return int(data.get('stock') or 0)

def api_set_variant_stock(store_id, token, product_id, variant_id, new_stock):
    if DRY_RUN:
        return
    url  = f"https://api.tiendanube.com/v1/{store_id}/products/{product_id}/variants/{variant_id}"
    body = json.dumps({"stock": new_stock}).encode('utf-8')
    req  = urllib.request.Request(
        url, data=body, method='PUT',
        headers={
            "Authentication": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "RetroClub-Sync/1.0"
        })
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


# ── Main ──────────────────────────────────────────────────────────────────────

# Cargar mapping — solo entradas confirmadas con par en ambas tiendas
with open(MAP_FILE, encoding='utf-8') as f:
    full_map = json.load(f)

mapping = [
    m for m in full_map
    if m.get('confirmed') and m.get('store_b') is not None
]

if not mapping:
    log("ERROR: No hay variantes confirmadas en variant_map.json", force=True)
    log("       Correr build_map.py primero y revisar el archivo.", force=True)
    sys.exit(1)

# Cargar estado previo
try:
    with open(STATE_FILE, encoding='utf-8') as f:
        state = json.load(f)
except:
    state = {}

mode_str = "INIT" if INIT_MODE else ("DRY-RUN" if DRY_RUN else "SYNC")
log(f"=== {mode_str} — {len(mapping)} variantes mapeadas ===", force=True)

new_state   = {}
updates_a   = 0
updates_b   = 0
errors      = 0
diffs_found = 0

for entry in mapping:
    vid_a = entry['store_a']['variant_id']
    vid_b = entry['store_b']['variant_id']
    pid_a = entry['store_a']['product_id']
    pid_b = entry['store_b']['product_id']
    label = f"{entry['model_a'][:35]:35s} {entry['talle']:4s}/{entry['player'][:15]}"
    key   = f"{vid_a}:{vid_b}"

    try:
        cur_a = api_get_variant(STORE_A_ID, STORE_A_TOKEN, pid_a, vid_a)
        time.sleep(API_DELAY)
        cur_b = api_get_variant(STORE_B_ID, STORE_B_TOKEN, pid_b, vid_b)
        time.sleep(API_DELAY)

        # ── INIT: R10 es fuente de verdad ──────────────────────────────────
        if INIT_MODE or key not in state:
            pool = cur_a
            if cur_b != pool:
                log(f"  INIT {label}: A={cur_a} B={cur_b} → set B={pool}")
                api_set_variant_stock(STORE_B_ID, STORE_B_TOKEN, pid_b, vid_b, pool)
                time.sleep(API_DELAY)
                updates_b += 1
            else:
                if VERBOSE:
                    log(f"  INIT {label}: A=B={pool} OK")
            new_state[key] = {'pool': pool, 'last_a': pool, 'last_b': pool}

        # ── SYNC NORMAL ────────────────────────────────────────────────────
        else:
            prev   = state[key]
            last_a = prev['last_a']
            last_b = prev['last_b']
            pool   = prev['pool']

            sold_a    = max(0, last_a - cur_a)
            sold_b    = max(0, last_b - cur_b)
            restock_a = max(0, cur_a - last_a)  # llego mercaderia a R10

            new_pool = max(0, pool - sold_a - sold_b + restock_a)

            changed = sold_a > 0 or sold_b > 0 or restock_a > 0

            if changed:
                diffs_found += 1
                log(f"  DELTA {label}: "
                    f"pool={pool} sold_A={sold_a} sold_B={sold_b} repo_A={restock_a} "
                    f"→ new_pool={new_pool}")

            need_update_a = cur_a != new_pool
            need_update_b = cur_b != new_pool

            if need_update_a:
                if VERBOSE:
                    log(f"    → set A={new_pool} (era {cur_a})")
                api_set_variant_stock(STORE_A_ID, STORE_A_TOKEN, pid_a, vid_a, new_pool)
                time.sleep(API_DELAY)
                updates_a += 1

            if need_update_b:
                if VERBOSE:
                    log(f"    → set B={new_pool} (era {cur_b})")
                api_set_variant_stock(STORE_B_ID, STORE_B_TOKEN, pid_b, vid_b, new_pool)
                time.sleep(API_DELAY)
                updates_b += 1

            new_state[key] = {'pool': new_pool, 'last_a': new_pool, 'last_b': new_pool}

    except urllib.error.HTTPError as e:
        log(f"  HTTP {e.code} — {label}: {e.reason}", force=True)
        errors += 1
        if key in state:
            new_state[key] = state[key]

    except Exception as e:
        log(f"  ERROR — {label}: {e}", force=True)
        errors += 1
        if key in state:
            new_state[key] = state[key]

# Guardar estado
if not DRY_RUN:
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(new_state, f, ensure_ascii=False, indent=2)

summary = (
    f"Completado — deltas={diffs_found} | "
    f"updates A={updates_a} B={updates_b} | "
    f"errores={errors}"
    + (" [DRY-RUN: nada fue modificado]" if DRY_RUN else "")
)
log(summary, force=True)
