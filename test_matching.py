import math

def _norm_gender(g):
    aliases = {'male':'Men','female':'Women','other':'Other','men':'Men','women':'Women','m':'Men','f':'Women'}
    return aliases.get(g.lower().strip(), g)

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def find_matches(me, all_profiles, radius_km=50):
    me_lat = float(me['lat']); me_lon = float(me['lon'])
    my_gender = _norm_gender(me.get('gender',''))
    my_prefs = _norm_gender(me.get('preferred_gender',''))
    pref_pool = {'Men','Women','Other'} if my_prefs == 'Everyone' else {my_prefs}
    matches = []
    for uid, other in all_profiles.items():
        if uid == me.get('_uid'): continue
        if not other.get('lat') or not other.get('lon'): continue
        other_gender = _norm_gender(other.get('gender',''))
        other_prefs = _norm_gender(other.get('preferred_gender',''))
        if other_gender not in pref_pool: continue
        interested = True if other_prefs == 'Everyone' else my_gender in {other_prefs}
        if not interested: continue
        dist = haversine_km(me_lat, me_lon, float(other['lat']), float(other['lon']))
        if dist > radius_km: continue
        matches.append({**other, 'uid': uid, 'distance_km': round(dist, 1)})
    matches.sort(key=lambda m: m['distance_km'])
    return matches

profiles = {
    1: {'name':'Alice','age':'25','gender':'Female','preferred_gender':'Men', 'lat':'12.9344','lon':'77.6101'},
    2: {'name':'Bob','age':'28','gender':'Male','preferred_gender':'Women', 'lat':'12.9716','lon':'77.5946'},
    3: {'name':'Charlie','age':'30','gender':'Male','preferred_gender':'Men', 'lat':'12.9352','lon':'77.6245'},
    4: {'name':'Diana','age':'27','gender':'Female','preferred_gender':'Everyone', 'lat':'12.9000','lon':'77.6000'},
    5: {'name':'Eve','age':'22','gender':'Female','preferred_gender':'Men', 'lat':'13.2000','lon':'77.7000'},
}

tests_passed = 0

m = find_matches({**profiles[1], '_uid': 1}, profiles)
assert any(m2['uid'] == 2 for m2 in m), 'Alice should match Bob'
tests_passed += 1
print(f'PASS {tests_passed}: Mutual match (Alice <-> Bob)')

m = find_matches({**profiles[1], '_uid': 1}, profiles)
assert not any(m2['uid'] == 3 for m2 in m), 'Alice should NOT match Charlie'
tests_passed += 1
print(f'PASS {tests_passed}: Gender mismatch (Alice likes Men, Charlie is Male not Men)')

m = find_matches({**profiles[2], '_uid': 2}, profiles)
assert any(m2['uid'] == 4 for m2 in m), 'Bob should match Diana (Everyone)'
tests_passed += 1
print(f'PASS {tests_passed}: Everyone preference (Diana likes Everyone)')

m = find_matches({**profiles[1], '_uid': 1}, profiles)
assert not any(m2['uid'] == 5 for m2 in m), 'Eve is too far (>50km)'
tests_passed += 1
print(f'PASS {tests_passed}: Distance filter (>50km rejected)')

profiles_without_loc = {**profiles, 6: {'name':'Frank','age':'30','gender':'Male','preferred_gender':'Women'}}
m = find_matches({**profiles[1], '_uid': 1}, profiles_without_loc)
assert not any(m2.get('uid') == 6 for m2 in m), 'Frank has no location'
tests_passed += 1
print(f'PASS {tests_passed}: Missing location skipped')

print(f'\n=== All {tests_passed}/5 matching tests passed! ===')
