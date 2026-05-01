"""Live deployment test — run against Railway bot."""
import requests, json, warnings, socket, time
warnings.filterwarnings('ignore')

# Hardcode DNS to avoid macOS flakiness
_orig_getaddrinfo = socket.getaddrinfo
def _patched(host, port, *args, **kwargs):
    if 'railway.app' in str(host):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('151.101.2.15', port))]
    return _orig_getaddrinfo(host, port, *args, **kwargs)
socket.getaddrinfo = _patched

BASE = 'https://web-production-c70c0.up.railway.app'
s = requests.Session()
s.headers.update({'Content-Type': 'application/json'})
s.verify = False

PASS = 0; FAIL = 0; results = []

def check(label, r, expect_key=None, expect_val=None):
    global PASS, FAIL
    try:
        d = r.json()
        ok = (d.get(expect_key) == expect_val) if expect_key else r.status_code < 400
        symbol = 'PASS' if ok else 'FAIL'
        if ok: PASS += 1
        else: FAIL += 1
        results.append((symbol, label, json.dumps(d)[:110]))
        return d
    except Exception as e:
        FAIL += 1
        results.append(('FAIL', label, str(e)[:80]))
        return {}

def post(path, body): return s.post(f'{BASE}{path}', json=body, timeout=60)
def get(path): return s.get(f'{BASE}{path}', timeout=15)
def ctx(scope, cid, payload, v=1):
    return post('/v1/context', {'scope':scope,'context_id':cid,'version':v,'payload':payload,'delivered_at':'2026-04-30T15:00:00Z'})
def wipe(): post('/v1/teardown', {})

# ── 1. HEALTHZ / METADATA ─────────────────────────────────────────
print('\n=== 1. HEALTHZ / METADATA ===')
check('healthz ok', get('/v1/healthz'), 'status', 'ok')
check('metadata team_name', get('/v1/metadata'), 'team_name', 'Ritesh Tiwari')
check('metadata contact_email', get('/v1/metadata'), 'contact_email', 'ritiktiwari2212@gmail.com')

# ── 2. TEARDOWN ───────────────────────────────────────────────────
print('\n=== 2. TEARDOWN ===')
check('teardown wipes', post('/v1/teardown', {}), 'status', 'wiped')

# ── 3. CONTEXT ────────────────────────────────────────────────────
print('\n=== 3. CONTEXT ===')
wipe()
for scope in ['category','merchant','customer','trigger']:
    check(f'context {scope} accepted', ctx(scope, f'c_{scope}', {'x':1}), 'accepted', True)
check('invalid scope rejected', post('/v1/context', {'scope':'bad','context_id':'x','version':1,'payload':{},'delivered_at':'2026-04-30T15:00:00Z'}), 'accepted', False)
check('same-version re-push accepted', ctx('category','c_category',{}), 'accepted', True)
check('version upgrade accepted', ctx('category','c_category',{'v':2},v=2), 'accepted', True)

# ── 4. TICK — edge cases (no LLM) ────────────────────────────────
print('\n=== 4. TICK edge cases ===')
wipe()
check('tick empty list', post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':[]}), 'actions', [])
check('tick unknown trigger', post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':['bogus_trg']}), 'actions', [])

# Missing merchant
ctx('category','restaurants',{'category_name':'Restaurants','voice':'lively'})
ctx('trigger','trg_nomrch',{'trigger_id':'trg_nomrch','merchant_id':'m_missing','kind':'low_orders','urgency':3,'suppression_key':'sk_nm'})
check('tick missing merchant -> skip', post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':['trg_nomrch']}), 'actions', [])

# Missing category
wipe()
ctx('merchant','m_nc',{'merchant_id':'m_nc','merchant_name':'X','category_slug':'gyms'})
ctx('trigger','trg_nocat',{'trigger_id':'trg_nocat','merchant_id':'m_nc','kind':'low_orders','urgency':3,'suppression_key':'sk_nc'})
check('tick missing category -> skip', post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':['trg_nocat']}), 'actions', [])

# Customer trigger without customer context
wipe()
ctx('category','salons',{'category_name':'Salons','voice':'warm'})
ctx('merchant','m_sal',{'merchant_id':'m_sal','merchant_name':'Glow','category_slug':'salons','city':'Mumbai'})
ctx('trigger','trg_cust',{'trigger_id':'trg_cust','merchant_id':'m_sal','customer_id':'cust_999','kind':'winback','urgency':4,'suppression_key':'sk_c','scope':'customer'})
check('tick customer trigger no customer ctx -> skip', post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':['trg_cust']}), 'actions', [])

# Blocked merchant
wipe()
ctx('category','restaurants',{'category_name':'Restaurants','voice':'lively'})
ctx('merchant','m_blk',{'merchant_id':'m_blk','merchant_name':'Blocked','category_slug':'restaurants','city':'Delhi'})
ctx('trigger','trg_blk',{'trigger_id':'trg_blk','merchant_id':'m_blk','kind':'low_orders','urgency':5,'suppression_key':'sk_blk'})
post('/v1/reply',{'conversation_id':'conv_blk_pre','merchant_id':'m_blk','customer_id':None,'from_role':'merchant','message':'STOP','received_at':'2026-04-30T15:00:00Z','turn_number':1})
check('tick blocked merchant -> skip', post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':['trg_blk']}), 'actions', [])

# ── 5. REPLY — deterministic cases ───────────────────────────────
print('\n=== 5. REPLY deterministic ===')

# Opt-out phrases
for phrase in ['stop','unsubscribe','band karo','nahi chahiye','mat bhejo','remove me','do not contact']:
    wipe()
    r = post('/v1/reply',{'conversation_id':f'conv_opt_{phrase[:4]}','merchant_id':'m_op','customer_id':None,'from_role':'merchant','message':phrase,'received_at':'2026-04-30T15:00:00Z','turn_number':1})
    check(f'opt-out [{phrase}]', r, 'action', 'end')

# Ended conversation
wipe()
post('/v1/reply',{'conversation_id':'conv_end','merchant_id':'m_e','customer_id':None,'from_role':'merchant','message':'stop','received_at':'2026-04-30T15:00:00Z','turn_number':1})
r = post('/v1/reply',{'conversation_id':'conv_end','merchant_id':'m_e','customer_id':None,'from_role':'merchant','message':'hello','received_at':'2026-04-30T15:00:00Z','turn_number':2})
check('ended conv reply -> end', r, 'action', 'end')

# Auto-reply state machine
wipe()
auto_msg = 'Thank you for contacting us. We will get back to you shortly.'
d1 = check('auto-reply turn=1 -> send/wait', post('/v1/reply',{'conversation_id':'conv_ar','merchant_id':'m_ar','customer_id':None,'from_role':'merchant','message':auto_msg,'received_at':'2026-04-30T15:00:00Z','turn_number':1}))
d2 = check('auto-reply turn=2 -> wait', post('/v1/reply',{'conversation_id':'conv_ar','merchant_id':'m_ar','customer_id':None,'from_role':'merchant','message':auto_msg,'received_at':'2026-04-30T15:00:00Z','turn_number':2}), 'action', 'wait')
d3 = check('auto-reply turn=3 -> end', post('/v1/reply',{'conversation_id':'conv_ar','merchant_id':'m_ar','customer_id':None,'from_role':'merchant','message':auto_msg,'received_at':'2026-04-30T15:00:00Z','turn_number':3}), 'action', 'end')

# ── 6. TICK + REPLY with LLM ─────────────────────────────────────
print('\n=== 6. TICK + REPLY with LLM (may be slow) ===')
wipe()
ctx('category','restaurants',{
    'slug':'restaurants','display_name':'Restaurants',
    'voice':{'tone':'lively_commercial','register':'casual_operator','code_mix':'none',
             'vocab_allowed':['covers','AOV','footfall','BOGO','match-night','lunch rush'],
             'vocab_taboo':['cheap','discount'],
             'salutation_examples':['Suresh','Mukesh','Anand'],
             'tone_examples':['Quick heads-up Suresh — IPL tonight shifts -12% covers']},
    'peer_stats':{'avg_rating':4.1,'avg_views_30d':5100,'avg_calls_30d':18,'avg_ctr':0.028,'avg_review_count':180,'retention_6mo_pct':0.55,'avg_post_freq_days':7},
    'offer_catalog':[{'title':'Pizza BOGO @ ₹499'},{'title':'Family Combo @ ₹799'}],
    'digest':[],
    'seasonal_beats':[{'month_range':'Mar-May','note':'IPL season — delivery +28%, dine-in -15%'}],
    'trend_signals':[],'regulatory_authorities':['FSSAI'],'professional_journals':['FoodService India']
})
ctx('merchant','m_rest',{
    'merchant_id':'m_rest','category_slug':'restaurants',
    'identity':{'name':'Spice Garden','city':'Delhi','locality':'Lajpat Nagar','verified':True,'languages':['en'],'owner_first_name':'Suresh'},
    'subscription':{'plan':'Basic','days_remaining':45,'status':'active'},
    'performance':{'views':4200,'calls':8,'directions':12,'ctr':0.018,'leads':3,'delta_7d':{'views_pct':-0.22,'calls_pct':-0.30,'ctr_pct':-0.02}},
    'offers':[{'title':'Butter Chicken @ ₹320','status':'active'},{'title':'Dal Makhani @ ₹220','status':'active'}],
    'signals':['Orders down 32% vs last month','3 new competitors opened nearby'],
    'customer_aggregate':{'total_unique_ytd':820,'delivery_orders_30d':280,'dine_in_orders_30d':45},
    'review_themes':[],'conversation_history':[]
})
ctx('trigger','trg_rest',{
    'trigger_id':'trg_rest','merchant_id':'m_rest','kind':'perf_dip','scope':'merchant','source':'internal','urgency':5,
    'suppression_key':'spice_perf_dip_apr',
    'expires_at':'2026-05-07T00:00:00Z',
    'payload':{'metric':'orders','delta_pct':-0.32,'period_days':30,'peer_delta_pct':-0.08}
})

print('  [INFO] Calling tick with LLM...')
t0 = time.time()
r = post('/v1/tick', {'now':'2026-04-30T15:00:00Z','available_triggers':['trg_rest']})
dt = time.time()-t0
d = r.json()
actions = d.get('actions',[])
print(f'  [INFO] tick took {dt:.1f}s, got {len(actions)} action(s)')
if actions:
    a = actions[0]
    body_preview = a.get('body','')[:120]
    print(f'  [INFO] body: {body_preview}')
    send_as = a.get('send_as'); cta = a.get('cta')
    print(f'  [INFO] send_as={send_as}, cta={cta}')
    conv_id = a.get('conversation_id','')
    has_body = bool(a.get('body','').strip())
    has_conv = bool(conv_id)
    no_url = 'http' not in a.get('body','').lower()
    check('tick LLM -> got action', r)
    if has_body: PASS+=1; results.append(('PASS','action has body',''))
    else: FAIL+=1; results.append(('FAIL','action has body',''))
    if no_url: PASS+=1; results.append(('PASS','no URL in body',''))
    else: FAIL+=1; results.append(('FAIL','no URL in body - found http!',''))

    # Reply to the LLM-generated conversation
    print('  [INFO] Testing reply to LLM conversation...')
    r2 = post('/v1/reply',{'conversation_id':conv_id,'merchant_id':'m_rest','customer_id':None,'from_role':'merchant','message':'Interesting, tell me more about what I can do.','received_at':'2026-04-30T15:00:00Z','turn_number':1})
    d2 = check('reply to LLM conv -> send', r2, 'action', 'send')
    if d2.get('action')=='send':
        reply_preview = d2.get('body','')[:120]
        print(f'  [INFO] reply body: {reply_preview}')
else:
    FAIL+=1; results.append(('FAIL','tick LLM produced 0 actions',''))

# ── PRINT SUMMARY ─────────────────────────────────────────────────
print('\n' + '='*60)
print(f'TOTAL: {PASS} PASS, {FAIL} FAIL')
print('='*60)
for symbol, label, detail in results:
    marker = '✓' if symbol=='PASS' else '✗'
    print(f'  {marker} {label}')
    if symbol=='FAIL' and detail:
        print(f'      → {detail}')
