import urllib.request, json, time
base = 'http://127.0.0.1:8000'

def get(u, timeout=60):
    try:
        with urllib.request.urlopen(base + u, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {'__err': str(e)}

def post(u, body, timeout=30):
    req = urllib.request.Request(base + u, data=json.dumps(body).encode(),
                                  headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

for _ in range(12):
    try:
        get('/health'); break
    except Exception:
        time.sleep(0.5)

print('=== /holdings (中国绿宝应被自动剔除) ===')
h = get('/holdings')
print('  stocks:', [x['name'] for x in h['data']['stocks']])

print('=== /dividend HK.00001 (长和 股息率3.14%) ===')
dv = get('/dividend?code=HK.00001')
print('  yield:', dv['data'].get('yield_ratio'), '| score:', dv['data'].get('score'), '| label:', dv['data'].get('label'))
print('=== /dividend HK.00700 (腾讯 1.12%) ===')
dv2 = get('/dividend?code=HK.00700')
print('  yield:', dv2['data'].get('yield_ratio'), '| score:', dv2['data'].get('score'), '| label:', dv2['data'].get('label'))

print('=== /earnings HK.00700 (8月=中报季) ===')
es = get('/earnings?code=HK.00700')
d = es['data']
print('  in_season:', d.get('in_season'), '| in_window:', d.get('in_window'),
      '| score:', d.get('score'), '| label:', d.get('label'), '| available:', d.get('available'))

print('=== /analyze HK.00001 reverse.details (含 dividend+earnings) ===')
a = get('/analyze?code=HK.00001')
rev = a['data'].get('reverse', {})
print('  reverse.score:', rev.get('score'))
print('  tiers:', list((rev.get('details') or {}).keys()))
print('  dividend:', (rev.get('details') or {}).get('dividend'))
print('  earnings:', (rev.get('details') or {}).get('earnings'))

print('=== /watchlist POST+GET ===')
w = post('/watchlist', {'code': 'HK.00001', 'name': '长和'})
print('  after add:', [x['code'] for x in w['data']])
g = get('/watchlist')
print('  GET watchlist:', [x['code'] for x in g['data']])
w2 = post('/watchlist', {'code': 'HK.00001', 'action': 'remove'})
print('  after remove:', [x['code'] for x in w2['data']])

print('=== /screener (默认龙头池) ===')
t = time.time()
sc = get('/screener?top_n=5')
print('  screener ok=%s elapsed=%.1fs count=%s' % (sc.get('ok'), time.time() - t,
      (sc.get('data') or {}).get('count')))
if sc.get('ok'):
    top = sc['data']['results'][0]
    print('  top:', top['code'], top['name'], 'total=', top.get('total_score'))
