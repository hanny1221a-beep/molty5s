"""
Strategy Brain — upgraded decision engine (v1.5.2 compatible).

Key upgrades vs original:
  SURVIVAL   — pre-escape pending DZ lebih agresif; pelarian ke hills/plains
               diprioritaskan; cek DZ SEBELUM pelarian guardian
  COMBAT     — stats guardian v1.5.2 akurat (HP 150, ATK 7, DEF 12);
               formula damage benar; threshold fight/flee dinamis per fase game
  ECONOMY    — 5 guardian × 120 sMoltz = 600; Moltz token selalu dipickup duluan;
               stockpile healing item cerdas (simpan Medkit utk endgame)
  TURN LOOP  — free actions (pickup → equip) SEBELUM cooldown action;
               rest hanya jika benar-benar aman & EP < threshold adaptif
  COMMUNICATION — talk & whisper untuk aliansi early-game & intimidasi late-game;
               broadcast via Megaphone/station untuk deterrence

v1.5.2 perubahan penting:
  - Curse DINONAKTIFKAN sementara (guardian tidak lagi freeze EP)
  - Guardian MENYERANG player agent secara langsung (treat as hostile)
  - Free room: 5 guardians (dari 30), tiap drop 120 sMoltz
  - Guardian stats: HP 150, ATK 7, DEF 12 (berbeda dari asumsi lama!)
  - connectedRegions: bisa full object ATAU bare string ID → type-check wajib
  - pendingDeathzones: entries adalah {id, name} objects
  - move EP cost: 2 base, 3 jika storm ATAU water terrain
  - explore action SUDAH DIHAPUS dari game (jangan kirim!)
  - Whisper ke target harus di region yang SAMA
"""

from bot.utils.logger import get_logger

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════════════════
# KONSTANTA STATS — data akurat dari combat-items.md v1.5.2
# ══════════════════════════════════════════════════════════════════════

WEAPONS = {
    "fist":   {"bonus": 0,  "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword":  {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow":    {"bonus": 5,  "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

WEAPON_PRIORITY = ["katana", "sniper", "sword", "pistol", "dagger", "bow", "fist"]

MONSTERS = {
    "wolf":   {"hp": 25, "atk": 15, "def": 1},
    "bear":   {"hp": 30, "atk": 12, "def": 3},
    "bandit": {"hp": 40, "atk": 25, "def": 5},
}

# Guardian stats v1.5.2 — DEF 12 jauh lebih tinggi dari versi lama!
GUARDIAN = {"hp": 150, "atk": 7, "def": 12}

RECOVERY = {
    "medkit":         {"hp": 50, "ep": 0},
    "bandage":        {"hp": 30, "ep": 0},
    "emergency_food": {"hp": 20, "ep": 0},
    "energy_drink":   {"hp": 0,  "ep": 5},
}

TERRAIN_VISION = {"hills": +2, "plains": +1, "ruins": 0, "forest": -1, "water": 0}
WEATHER_COMBAT_PENALTY = {"clear": 0.0, "rain": 0.05, "fog": 0.10, "storm": 0.15}

PICKUP_PRIORITY = {
    "rewards": 300, "katana": 120, "sniper": 115, "sword": 110,
    "pistol": 105, "dagger": 100, "bow": 95, "medkit": 80,
    "bandage": 75, "emergency_food": 70, "energy_drink": 65,
    "binoculars": 60, "map": 55, "megaphone": 45, "radio": 40,
}

# ══════════════════════════════════════════════════════════════════════
# STATE GAME
# ══════════════════════════════════════════════════════════════════════

_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": []}
_talked_regions: set = set()


def reset_game_state():
    """Reset tracking state per-game. Panggil saat game_ended."""
    global _known_agents, _map_knowledge, _talked_regions
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _talked_regions = set()
    log.info("Brain reset untuk game baru")


# ══════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

def _weapon_bonus(weapon_obj) -> int:
    if not weapon_obj or not isinstance(weapon_obj, dict):
        return 0
    return WEAPONS.get(weapon_obj.get("typeId", "").lower(), {}).get("bonus", 0)


def _weapon_range(weapon_obj) -> int:
    if not weapon_obj or not isinstance(weapon_obj, dict):
        return 0
    return WEAPONS.get(weapon_obj.get("typeId", "").lower(), {}).get("range", 0)


def _calc_damage(atk: int, weapon_bonus: int, target_def: int,
                 weather: str = "clear") -> int:
    """Formula: (ATK + bonus - DEF*0.5) * (1 - weather_penalty), min 1."""
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather.lower(), 0.0)
    return max(1, int(base * (1 - penalty)))


def _move_ep_cost(terrain: str, weather: str) -> int:
    """EP cost move: water=3, storm=3, lainnya=2."""
    if terrain.lower() == "water":
        return 3
    if weather.lower() == "storm":
        return 3
    return 2


def _estimate_enemy_bonus(agent_dict: dict) -> int:
    w = agent_dict.get("equippedWeapon")
    if not w or not isinstance(w, dict):
        return 0
    return WEAPONS.get(w.get("typeId", "").lower(), {}).get("bonus", 0)


def _resolve_region(entry, view: dict):
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry:
                return r
    return None


def _get_region_id(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id", "")
    return ""


def _is_in_range(target: dict, my_region: str, weapon_rng: int, connections) -> bool:
    target_region = target.get("regionId", "")
    if not target_region or target_region == my_region:
        return True
    if weapon_rng >= 1 and connections:
        adj = {_get_region_id(c) for c in connections}
        return target_region in adj
    return False


def _track_agents(visible_agents: list, my_id: str, my_region: str):
    global _known_agents
    for a in visible_agents:
        if not isinstance(a, dict):
            continue
        aid = a.get("id", "")
        if not aid or aid == my_id:
            continue
        _known_agents[aid] = {
            "hp": a.get("hp", 100),
            "atk": a.get("atk", 10),
            "def": a.get("def", 5),
            "isGuardian": a.get("isGuardian", False),
            "equippedWeapon": a.get("equippedWeapon"),
            "regionId": a.get("regionId", my_region),
            "isAlive": a.get("isAlive", True),
        }
    if len(_known_agents) > 60:
        dead = [k for k, v in _known_agents.items() if not v.get("isAlive", True)]
        for d in dead[:20]:
            del _known_agents[d]


# ══════════════════════════════════════════════════════════════════════
# SURVIVAL HELPERS
# ══════════════════════════════════════════════════════════════════════

def _find_best_safe_region(connections, danger_ids: set, view: dict) -> str | None:
    """Cari region escape terbaik — scoring terrain-aware."""
    candidates = []
    item_region_ids = set()
    for entry in view.get("visibleItems", []):
        if isinstance(entry, dict):
            rid = entry.get("regionId") or entry.get("item", {}).get("regionId", "")
            if rid:
                item_region_ids.add(rid)

    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                score = 1 + (3 if conn in item_region_ids else 0)
                candidates.append((conn, score))
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            terrain = conn.get("terrain", "").lower()
            score = {"hills": 4, "plains": 2, "ruins": 3, "forest": 1, "water": -3}.get(terrain, 0)
            if rid in item_region_ids:
                score += 3
            if rid in _map_knowledge.get("death_zones", set()):
                continue
            candidates.append((rid, score))

    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # Fallback: region manapun yang tidak aktif DZ
    for conn in connections:
        rid = _get_region_id(conn)
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            log.warning("Fallback escape ke %s", rid[:8])
            return rid
    return None


def _build_danger_ids(connections, pending_dz: list, view: dict) -> set:
    """Set semua region berbahaya: DZ aktif + pending DZ + map knowledge DZ."""
    danger = set()
    for dz in pending_dz:
        if isinstance(dz, dict):
            rid = dz.get("id", "")
        elif isinstance(dz, str):
            rid = dz
        else:
            continue
        if rid:
            danger.add(rid)
    for conn in connections:
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            danger.add(conn.get("id", ""))
    danger.update(_map_knowledge.get("death_zones", set()))
    return danger


# ══════════════════════════════════════════════════════════════════════
# COMBAT HELPERS
# ══════════════════════════════════════════════════════════════════════

def _evaluate_fight(my_atk: int, my_weapon, my_def: int, my_hp: int,
                    target: dict, weather: str) -> dict:
    """Evaluasi apakah worth bertarung vs target."""
    my_bonus = _weapon_bonus(my_weapon)
    t_hp = target.get("hp", 100)
    t_def = target.get("def", 5)
    t_atk = target.get("atk", 10)
    t_bonus = _estimate_enemy_bonus(target)

    my_dmg    = _calc_damage(my_atk, my_bonus, t_def, weather)
    enemy_dmg = _calc_damage(t_atk, t_bonus, my_def, weather)
    turns_to_kill = max(1, int(t_hp / my_dmg)) if my_dmg > 0 else 999
    damage_taken  = enemy_dmg * turns_to_kill
    will_survive  = my_hp - damage_taken > 15
    can_finish    = t_hp <= my_dmg * 3
    favorable     = my_dmg > enemy_dmg

    fight = will_survive or can_finish or (favorable and my_hp > 40)
    return {"fight": fight, "my_dmg": my_dmg, "enemy_dmg": enemy_dmg,
            "turns_to_kill": turns_to_kill, "will_survive": will_survive}


def _guardian_fight_worth(my_atk: int, my_weapon, my_def: int, my_hp: int,
                          weather: str) -> dict:
    """
    Evaluasi fight vs guardian v1.5.2.
    Guardian: HP 150, ATK 7, DEF 12 → damage kita dikurangi 6!
    Reward: 120 sMoltz per kill (sangat worth jika bisa menang).
    """
    my_bonus  = _weapon_bonus(my_weapon)
    my_dmg    = _calc_damage(my_atk, my_bonus, GUARDIAN["def"], weather)
    enemy_dmg = _calc_damage(GUARDIAN["atk"], 0, my_def, weather)
    turns_to_kill    = max(1, int(GUARDIAN["hp"] / my_dmg)) if my_dmg > 0 else 999
    estimated_damage = enemy_dmg * turns_to_kill
    hp_after         = max(0, my_hp - estimated_damage)
    worth = hp_after > 20 and my_dmg >= 2
    return {"worth": worth, "my_dmg": my_dmg, "enemy_dmg": enemy_dmg,
            "turns_to_kill": turns_to_kill, "estimated_hp_after": hp_after}


def _select_best_target(targets: list) -> dict:
    """Pilih target HP terendah (tiebreaker: ATK terendah)."""
    return min(targets, key=lambda t: (t.get("hp", 999), t.get("atk", 99)))


# ══════════════════════════════════════════════════════════════════════
# ECONOMY HELPERS
# ══════════════════════════════════════════════════════════════════════

def _check_pickup(visible_items: list, inventory: list, region_id: str) -> dict | None:
    """Pickup item terbaik di region saat ini (FREE action)."""
    if len(inventory) >= 10:
        return None

    local_items = []
    for entry in visible_items:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            item = dict(inner)
            item["regionId"] = entry.get("regionId", "")
        elif entry.get("id"):
            item = entry
        else:
            continue
        item_rid = item.get("regionId", "")
        if item_rid == region_id or not item_rid:
            local_items.append(item)

    if not local_items:
        return None

    heal_stock = sum(
        1 for i in inventory
        if isinstance(i, dict)
        and i.get("typeId", "").lower() in RECOVERY
        and RECOVERY.get(i.get("typeId", "").lower(), {}).get("hp", 0) > 0
    )
    best_inv_weapon = max(
        (WEAPONS.get(i.get("typeId", "").lower(), {}).get("bonus", 0)
         for i in inventory if isinstance(i, dict) and i.get("category") == "weapon"),
        default=0
    )

    def score(item: dict) -> int:
        type_id  = item.get("typeId", "").lower()
        category = item.get("category", "").lower()
        if type_id == "rewards" or category == "currency":
            return 300
        if category == "weapon":
            bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
            return (120 + bonus) if bonus > best_inv_weapon else 0
        if type_id == "binoculars":
            has = any(i.get("typeId", "").lower() == "binoculars"
                      for i in inventory if isinstance(i, dict))
            return 60 if not has else 0
        if type_id == "map":
            return 55
        if type_id in RECOVERY and RECOVERY[type_id].get("hp", 0) > 0:
            if heal_stock < 4:
                return PICKUP_PRIORITY.get(type_id, 50) + 10
            elif heal_stock < 6:
                return PICKUP_PRIORITY.get(type_id, 50)
            return 0
        if type_id == "energy_drink":
            return 65
        return PICKUP_PRIORITY.get(type_id, 0)

    local_items.sort(key=score, reverse=True)
    best = local_items[0]
    best_score = score(best)
    if best_score > 0:
        type_id = best.get("typeId", "item")
        log.info("PICKUP: %s (score=%d, inv=%d/10)", type_id, best_score, len(inventory))
        return {"action": "pickup", "data": {"itemId": best["id"]},
                "reason": f"PICKUP: {type_id}"}
    return None


def _check_equip(inventory: list, equipped) -> dict | None:
    """Auto-equip weapon terbaik dari inventory (FREE action)."""
    current_bonus = _weapon_bonus(equipped)
    best = None
    best_bonus = current_bonus
    for item in inventory:
        if not isinstance(item, dict) or item.get("category") != "weapon":
            continue
        bonus = WEAPONS.get(item.get("typeId", "").lower(), {}).get("bonus", 0)
        if bonus > best_bonus:
            best = item
            best_bonus = bonus
    if best:
        log.info("EQUIP: %s (bonus %d→%d)", best.get("typeId"), current_bonus, best_bonus)
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP: {best.get('typeId')} (ATK+{best_bonus})"}
    return None


def _use_utility_item(inventory: list, ep: int) -> dict | None:
    """Gunakan Map segera; Energy Drink hanya jika EP ≤ 1."""
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        if type_id == "map":
            log.info("UTILITY: Menggunakan Map!")
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": "UTILITY: Map — reveal peta untuk DZ tracking"}
        if type_id == "energy_drink" and ep <= 1:
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": f"UTILITY: Energy Drink (EP={ep} kritis, +5EP)"}
    return None


def _find_heal_item(inventory: list, critical: bool = False) -> dict | None:
    """critical=True: heal terbesar dulu; False: hemat Medkit untuk darurat."""
    heals = [
        i for i in inventory
        if isinstance(i, dict)
        and i.get("typeId", "").lower() in RECOVERY
        and RECOVERY.get(i.get("typeId", "").lower(), {}).get("hp", 0) > 0
    ]
    if not heals:
        return None
    heals.sort(
        key=lambda i: RECOVERY.get(i.get("typeId", "").lower(), {}).get("hp", 0),
        reverse=critical
    )
    return heals[0]


# ══════════════════════════════════════════════════════════════════════
# MOVEMENT HELPERS
# ══════════════════════════════════════════════════════════════════════

def _choose_move_target(connections, danger_ids: set, current_region: dict,
                        visible_items: list, alive_count: int,
                        view: dict) -> str | None:
    """Pilih region tujuan terbaik. Tidak pernah masuk ke DZ atau pending DZ."""
    candidates = []
    item_region_ids = set()
    for entry in visible_items:
        if isinstance(entry, dict):
            rid = entry.get("regionId") or entry.get("item", {}).get("regionId", "")
            if rid:
                item_region_ids.add(rid)

    for conn in connections:
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            score = 1 + (5 if conn in item_region_ids else 0)
            candidates.append((conn, score))

        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue
            if rid in _map_knowledge.get("death_zones", set()):
                continue

            terrain = conn.get("terrain", "").lower()
            weather = conn.get("weather", "").lower()
            score = {"hills": 4, "plains": 2, "ruins": 3, "forest": 1, "water": -3}.get(terrain, 0)

            if rid in item_region_ids:
                score += 6

            facs = conn.get("interactables", [])
            unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
            score += len(unused) * 3

            weather_bonus = {"clear": 1, "rain": 0, "fog": -2, "storm": -3}
            score += weather_bonus.get(weather, 0)

            if alive_count < 25:
                score += 2
                if rid in _map_knowledge.get("safe_center", []):
                    score += 4

            candidates.append((rid, score))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    log.debug("Move candidates: %s", [(c[:8], s) for c, s in candidates[:3]])
    return candidates[0][0]


def _select_facility(interactables: list, hp: int, ep: int,
                     in_death_zone: bool) -> dict | None:
    """Pilih facility terbaik. Tidak bisa interact di DZ!"""
    if in_death_zone:
        return None
    priority = [
        ("medical_facility",   lambda: hp < 80),
        ("supply_cache",       lambda: True),
        ("watchtower",         lambda: True),
        ("broadcast_station",  lambda: True),
    ]
    for fac_type, condition in priority:
        for fac in interactables:
            if not isinstance(fac, dict) or fac.get("isUsed"):
                continue
            if fac.get("type", "").lower() == fac_type and condition():
                return fac
    return None


# ══════════════════════════════════════════════════════════════════════
# KOMUNIKASI
# ══════════════════════════════════════════════════════════════════════

def _check_communication(view: dict, my_id: str, my_hp: int, my_ep: int,
                         alive_count: int, region_id: str) -> dict | None:
    """
    Komunikasi strategis (FREE action):
    - Balas whisper private
    - Tawaran aliansi early-game (1x per region)
    - Intimidasi late-game
    """
    global _talked_regions
    messages      = view.get("recentMessages", [])
    visible_agents = view.get("visibleAgents", [])

    # SELALU balas whisper private
    for msg in messages:
        if (msg.get("type") == "private"
                and msg.get("senderId") != my_id
                and msg.get("targetId") == my_id):
            sender_id = msg.get("senderId", "")
            if sender_id:
                reply = f"HP:{my_hp} EP:{my_ep}. Setuju cooperate. Fokus guardian dulu."
                log.info("WHISPER balas ke %s", sender_id[:8])
                return {"action": "whisper",
                        "data": {"targetId": sender_id, "message": reply[:200]},
                        "reason": "COMMS: Balas private message"}

    # Agents manusia di region sama
    agents_here = [a for a in visible_agents
                   if isinstance(a, dict) and a.get("isAlive")
                   and a.get("regionId") == region_id
                   and not a.get("isGuardian", False)
                   and a.get("id") != my_id]

    if agents_here and region_id not in _talked_regions:
        _talked_regions.add(region_id)
        if alive_count > 50:
            msg_text = f"HP:{my_hp}. Cooperate? Guardian ada 5 di map, kill dulu = 120 sMoltz!"
        elif alive_count < 15:
            msg_text = f"Tersisa {alive_count}. Siap menghadapi saya?"
        else:
            msg_text = f"HP:{my_hp} EP:{my_ep}. Kita hindari saling bunuh dulu."
        return {"action": "talk",
                "data": {"message": msg_text[:200]},
                "reason": f"COMMS: {'Aliansi early' if alive_count > 50 else 'Strategi mid'}-game"}

    return None


# ══════════════════════════════════════════════════════════════════════
# MAP LEARNING
# ══════════════════════════════════════════════════════════════════════

def learn_from_map(view: dict):
    """Panggil setelah Map digunakan — pelajari seluruh layout peta."""
    global _map_knowledge
    visible_regions = view.get("visibleRegions", [])
    if not visible_regions:
        return
    _map_knowledge["revealed"] = True
    safe_regions = []
    for region in visible_regions:
        if not isinstance(region, dict):
            continue
        rid = region.get("id", "")
        if not rid:
            continue
        if region.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            conns = region.get("connections", [])
            terrain = region.get("terrain", "").lower()
            tv = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(terrain, 0)
            safe_regions.append((rid, len(conns) + tv))
    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]
    log.info("MAP LEARNED: %d DZ, top center: %s",
             len(_map_knowledge["death_zones"]),
             [r[:8] for r in _map_knowledge["safe_center"][:3]])


# ══════════════════════════════════════════════════════════════════════
# MAIN DECISION ENGINE
# ══════════════════════════════════════════════════════════════════════

def decide_action(view: dict, can_act: bool, memory_temp: dict = None) -> dict | None:
    """
    Engine keputusan utama. Return action dict atau None.

    URUTAN PRIORITAS:
    [FREE]  0a. Pickup item di region
    [FREE]  0b. Equip weapon terbaik
    [FREE]  0c. Komunikasi strategis
    [MAIN]  1.  ESCAPE death zone aktif
    [MAIN]  1b. PRE-ESCAPE pending death zone
    [MAIN]  2.  GUARDIAN FLEE (HP < 50 dan tidak worth fight)
    [MAIN]  3.  CRITICAL HEAL (HP < 30)
    [MAIN]  3b. Utility items (Map, Energy Drink)
    [MAIN]  4.  GUARDIAN FIGHT (120 sMoltz! Hanya jika worth)
    [MAIN]  5.  ENEMY COMBAT (adaptif berdasarkan alive_count)
    [MAIN]  6.  MONSTER FARMING
    [MAIN]  7.  MODERATE HEAL (HP < 70, area aman)
    [MAIN]  8.  FACILITY INTERACTION
    [MAIN]  9.  STRATEGIC MOVEMENT
    [MAIN]  10. REST (EP < threshold adaptif)
    """
    self_data   = view.get("self", {})
    region      = view.get("currentRegion", {})
    hp          = self_data.get("hp", 100)
    ep          = self_data.get("ep", 10)
    max_ep      = self_data.get("maxEp", 10)
    atk         = self_data.get("atk", 10)
    defense     = self_data.get("def", 5)
    is_alive    = self_data.get("isAlive", True)
    inventory   = self_data.get("inventory", [])
    equipped    = self_data.get("equippedWeapon")
    my_id       = self_data.get("id", "")

    visible_agents   = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_items    = view.get("visibleItems", [])
    connected_regs   = view.get("connectedRegions", [])
    pending_dz       = view.get("pendingDeathzones", [])
    alive_count      = view.get("aliveCount", 100)

    region_id      = region.get("id", "") if isinstance(region, dict) else ""
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "clear").lower() if isinstance(region, dict) else "clear"
    in_dz          = region.get("isDeathZone", False) if isinstance(region, dict) else False
    interactables  = region.get("interactables", []) if isinstance(region, dict) else []

    if not is_alive:
        return None

    connections = connected_regs or region.get("connections", [])
    danger_ids  = _build_danger_ids(connections, pending_dz, view)
    move_ep     = _move_ep_cost(region_terrain, region_weather)

    _track_agents(visible_agents, my_id, region_id)

    # ── [FREE] 0a: Pickup ────────────────────────────────────────────
    pickup = _check_pickup(visible_items, inventory, region_id)
    if pickup:
        return pickup

    # ── [FREE] 0b: Equip ─────────────────────────────────────────────
    equip = _check_equip(inventory, equipped)
    if equip:
        return equip

    # ── [FREE] 0c: Komunikasi ────────────────────────────────────────
    comms = _check_communication(view, my_id, hp, ep, alive_count, region_id)
    if comms:
        return comms

    # ── Cooldown actions — cek can_act ───────────────────────────────
    if not can_act:
        return None

    # ── [MAIN] 1: ESCAPE Death Zone ──────────────────────────────────
    if in_dz and ep >= move_ep:
        safe = _find_best_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("DEATH ZONE! Escape ke %s (HP=%d)", safe[:8], hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE DZ: HP={hp}, 1.34HP/s damage!"}
        log.error("DI DZ tapi tidak ada escape!")

    # ── [MAIN] 1b: PRE-ESCAPE pending DZ ─────────────────────────────
    if region_id in danger_ids and ep >= move_ep and not in_dz:
        safe = _find_best_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("Pre-escape: region %s akan jadi DZ!", region_id[:8])
            return {"action": "move", "data": {"regionId": safe},
                    "reason": "PRE-ESCAPE: Region akan menjadi DZ"}

    # ── [MAIN] 2: GUARDIAN FLEE ──────────────────────────────────────
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    if guardians_here and hp < 50 and ep >= move_ep:
        g_eval = _guardian_fight_worth(atk, equipped, defense, hp, region_weather)
        if not g_eval["worth"]:
            safe = _find_best_safe_region(connections, danger_ids, view)
            if safe:
                log.warning("Guardian flee: HP=%d, g_dmg=%d/turn", hp, g_eval["enemy_dmg"])
                return {"action": "move", "data": {"regionId": safe},
                        "reason": f"GUARDIAN FLEE: HP={hp}, enemy_dmg={g_eval['enemy_dmg']}/turn"}

    # ── [MAIN] 3: CRITICAL HEAL ──────────────────────────────────────
    if hp < 30:
        heal = _find_heal_item(inventory, critical=True)
        if heal:
            type_id  = heal.get("typeId", "heal")
            heal_val = RECOVERY.get(type_id.lower(), {}).get("hp", 0)
            log.info("CRITICAL HEAL: HP=%d, %s +%dHP", hp, type_id, heal_val)
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"CRITICAL HEAL: HP={hp}, {type_id} +{heal_val}HP"}

    # ── [MAIN] 3b: Utility items ──────────────────────────────────────
    util = _use_utility_item(inventory, ep)
    if util:
        return util

    # ── [MAIN] 4: GUARDIAN FIGHT (120 sMoltz per kill!) ──────────────
    guardians_visible = [a for a in visible_agents
                         if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians_visible and ep >= 2 and hp >= 50:
        g_eval = _guardian_fight_worth(atk, equipped, defense, hp, region_weather)
        if g_eval["worth"]:
            weapon_rng = _weapon_range(equipped)
            in_range = [g for g in guardians_visible
                        if _is_in_range(g, region_id, weapon_rng, connections)]
            if in_range:
                target = _select_best_target(in_range)
                t_hp = target.get("hp", GUARDIAN["hp"])
                log.info("GUARDIAN FIGHT: HP=%d/150, my_dmg=%d, hp_after=%d (120 sMoltz!)",
                         t_hp, g_eval["my_dmg"], g_eval["estimated_hp_after"])
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": (f"GUARDIAN FARM: HP {t_hp}/150, "
                                   f"my_dmg={g_eval['my_dmg']}, "
                                   f"hp_after={g_eval['estimated_hp_after']} (120 sMoltz!)")}

    # ── [MAIN] 5: ENEMY COMBAT ────────────────────────────────────────
    hp_threshold = 45 if alive_count > 30 else 30
    enemies = [a for a in visible_agents
               if not a.get("isGuardian", False) and a.get("isAlive", True)
               and a.get("id") != my_id]
    if enemies and ep >= 2 and hp >= hp_threshold:
        weapon_rng = _weapon_range(equipped)
        in_range   = [e for e in enemies
                      if _is_in_range(e, region_id, weapon_rng, connections)]
        if in_range:
            target = _select_best_target(in_range)
            eval_r = _evaluate_fight(atk, equipped, defense, hp, target, region_weather)
            if eval_r["fight"]:
                log.info("COMBAT: HP=%d, my=%d vs their=%d, survive=%s",
                         target.get("hp", "?"), eval_r["my_dmg"],
                         eval_r["enemy_dmg"], eval_r["will_survive"])
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": (f"COMBAT: target HP={target.get('hp','?')}, "
                                   f"my={eval_r['my_dmg']} vs their={eval_r['enemy_dmg']}")}
            elif hp < 45 and ep >= move_ep:
                # Retreat jika fight tidak menguntungkan
                safe = _find_best_safe_region(connections, danger_ids, view)
                if safe:
                    log.info("RETREAT: fight unfavorable (my=%d vs their=%d)",
                             eval_r["my_dmg"], eval_r["enemy_dmg"])
                    return {"action": "move", "data": {"regionId": safe},
                            "reason": "RETREAT: fight unfavorable, reposition"}

    # ── [MAIN] 6: MONSTER FARMING ────────────────────────────────────
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2:
        weapon_rng = _weapon_range(equipped)
        in_range   = [m for m in monsters
                      if _is_in_range(m, region_id, weapon_rng, connections)]
        if in_range:
            target = _select_best_target(in_range)
            m_name  = target.get("typeId", target.get("name", "wolf")).lower()
            m_stats = MONSTERS.get(m_name, {"hp": 30, "atk": 15, "def": 2})
            my_dmg  = _calc_damage(atk, _weapon_bonus(equipped), m_stats["def"], region_weather)
            m_dmg   = _calc_damage(m_stats["atk"], 0, defense, region_weather)
            t_to_kill = max(1, int(target.get("hp", m_stats["hp"]) / my_dmg))
            dmg_taken = m_dmg * t_to_kill
            if hp - dmg_taken > 15:
                log.info("MONSTER: %s HP=%d, my_dmg=%d, dmg_taken~%d",
                         m_name, target.get("hp", "?"), my_dmg, dmg_taken)
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "monster"},
                        "reason": f"MONSTER: {m_name} HP={target.get('hp','?')}, dmg_taken~{dmg_taken}"}

    # ── [MAIN] 7: MODERATE HEAL ───────────────────────────────────────
    if hp < 70 and not enemies:
        heal = _find_heal_item(inventory, critical=(hp < 40))
        if heal:
            type_id  = heal.get("typeId", "heal")
            heal_val = RECOVERY.get(type_id.lower(), {}).get("hp", 0)
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, {type_id} +{heal_val}HP (area aman)"}

    # ── [MAIN] 8: FACILITY ───────────────────────────────────────────
    if interactables and ep >= 2 and not in_dz:
        fac = _select_facility(interactables, hp, ep, in_dz)
        if fac:
            log.info("FACILITY: %s", fac.get("type"))
            return {"action": "interact",
                    "data": {"interactableId": fac["id"]},
                    "reason": f"FACILITY: {fac.get('type')} (HP={hp})"}

    # ── [MAIN] 9: STRATEGIC MOVEMENT ────────────────────────────────
    if ep >= move_ep and connections:
        target_region = _choose_move_target(
            connections, danger_ids, region, visible_items, alive_count, view)
        if target_region:
            log.info("MOVE ke %s", target_region[:8])
            return {"action": "move", "data": {"regionId": target_region},
                    "reason": "EXPLORE: Bergerak ke posisi strategis"}

    # ── [MAIN] 10: REST ──────────────────────────────────────────────
    ep_threshold = 5 if alive_count < 20 else 4
    if (ep < ep_threshold and not enemies and not in_dz
            and region_id not in danger_ids):
        log.info("REST: EP=%d/%d, aman", ep, max_ep)
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep}, area aman (+1 bonus EP)"}

    return None
