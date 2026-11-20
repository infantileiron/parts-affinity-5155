"""联用规则计算：按设备分层的单侧精确检验。

口径：
* 每条含备件的有效作业是一个事务，重复备件在事务内去重；
* 候选规则不按支持度裁剪：A、B 不相交且 A∪B 不超过 3 项，
  从冻结样本出现过的备件全集枚举，保证多重校正范围固定；
* 对每条规则，先在每台设备内固定前件频数 a、后件频数 b，零假设下
  共同出现次数 X 服从超几何分布 Hypergeom(n, b, a)；
* 各设备相互独立，对超几何分布做卷积，得到总共同出现次数的精确分布；
* p 值为 P(X >= 实际共同出现次数)（单侧）；
* 对“同一台设备中两侧都曾出现”的全部候选（含零共现）做
  Benjamini-Hochberg 校正；支持度/置信度/提升度/显著性阈值只筛选
  展示与入表，不参与也不缩小校正范围。
"""
import hashlib
import json
import math
from itertools import combinations

VERSION = 'stratified-exact-max3-v2'
MAX_ITEMS = 3
EPS = 1e-12
SMALLEST_FLOAT = float.fromhex('0x0.0000000000001p-1022')


def _bh_qvalues(items):
    """对 items（均含 p_value）按 Benjamini-Hochberg 写入 q_value。"""
    ordered = sorted(items, key=lambda item: (item['p_value'], item['signature']))
    total = len(ordered)
    running = 1.0
    for index in range(total - 1, -1, -1):
        item = ordered[index]
        value = item['p_value'] * total / (index + 1)
        running = min(running, value)
        item['q_value'] = min(1.0, max(0.0, running))
    return items


def candidate_signatures(universe):
    """候选规则签名集合：只由冻结样本的备件全集决定。"""
    return {signature for _, _, signature in _candidate_rules(universe)}


def _candidate_rules(universe):
    """枚举 A∪B 大小为 2..3 的所有有序规则，不依赖任何筛选阈值。"""
    seen = set()
    for size in range(2, MAX_ITEMS + 1):
        for combo in combinations(sorted(universe), size):
            combo_set = set(combo)
            for antecedent_size in range(1, size):
                for antecedent in combinations(combo, antecedent_size):
                    consequent = tuple(sorted(combo_set - set(antecedent)))
                    signature = ','.join(antecedent) + '->' + ','.join(consequent)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    yield antecedent, consequent, signature


def _prepare(rows):
    """冻结样本 -> 按设备分组的有效事务，并记录空事务的排除情况。"""
    groups = {}
    equipment_order = []
    excluded_by_equipment = {}
    excluded = 0
    for row in rows:
        equipment = row.get('equipment') or '未标注'
        if equipment not in groups:
            groups[equipment] = []
            equipment_order.append(equipment)
            excluded_by_equipment[equipment] = 0
        parts = frozenset(row.get('parts') or [])
        if not parts:
            excluded += 1
            excluded_by_equipment[equipment] += 1
            continue
        groups[equipment].append(parts)
    return (
        [(equipment, groups[equipment]) for equipment in equipment_order],
        excluded,
        excluded_by_equipment,
    )


def _hypergeometric(n, a, b, cache):
    """返回 (分子表, 分母)，X 取值范围为 max(0,a+b-n)..min(a,b)。"""
    key = (n, a, b)
    if key not in cache:
        low = max(0, a + b - n)
        high = min(a, b)
        cache[key] = (
            {x: math.comb(b, x) * math.comb(n - b, a - x) for x in range(low, high + 1)},
            math.comb(n, a),
        )
    return cache[key]


def _exact_stratified(groups, antecedent, consequent, cache):
    """逐设备固定前后件频数，卷积合并后求单侧精确 p 值。"""
    distribution = {0: 1}
    denominator = 1
    observed = 0
    expected = 0.0
    in_scope = False
    per_equipment = []
    for equipment, transactions in groups:
        n = len(transactions)
        a = sum(antecedent <= transaction for transaction in transactions)
        b = sum(consequent <= transaction for transaction in transactions)
        both = sum((antecedent | consequent) <= transaction for transaction in transactions)
        observed += both
        stratum_expected = (a * b / n) if n else 0.0
        expected += stratum_expected
        # 只有同一台设备里前后件都出现过，才进入校正范围；零共现也算。
        contributes = a > 0 and b > 0
        if contributes:
            in_scope = True
            numerators, stratum_denominator = _hypergeometric(n, a, b, cache)
            merged = {}
            for partial, partial_count in distribution.items():
                for x, x_count in numerators.items():
                    merged[partial + x] = merged.get(partial + x, 0) + partial_count * x_count
            distribution = merged
            denominator *= stratum_denominator
        per_equipment.append({
            'equipment': equipment,
            'jobs': n,
            'a': a,
            'b': b,
            'both': both,
            'expected': stratum_expected,
            'contributes': contributes,
        })
    if not in_scope:
        return 1.0, observed, expected, per_equipment, in_scope
    tail = sum(count for x, count in distribution.items() if x >= observed)
    p_value = tail / denominator
    # 超大整数相除时极小概率可能下溢为 0，保留一个可排序的最小浮点值。
    if p_value == 0:
        p_value = SMALLEST_FLOAT
    return min(1.0, p_value), observed, expected, per_equipment, in_scope


def _counts(groups, itemset):
    return sum(
        itemset <= transaction
        for _, transactions in groups
        for transaction in transactions
    )


def minimum_count(valid_jobs, min_support):
    return math.ceil(valid_jobs * min_support - EPS)


def rule_reasons(rule, valid_jobs, min_support, min_confidence, min_lift, alpha):
    """按正式阈值复算失格原因；rule 可以是 ORM 对象或字典。

    以规则上的频数和精确检验结果为准，不依赖可能过期的 qualified 标记，
    因此收紧阈值与起草并发时也能挡住失格规则。
    """
    def get(name, default=None):
        if isinstance(rule, dict):
            return rule.get(name, default)
        return getattr(rule, name, default)

    reasons = []
    count = int(get('count') or 0)
    support = float(get('support') or 0.0)
    confidence = float(get('confidence') or 0.0)
    lift = float(get('lift') or 0.0)
    q_value = get('q_value')
    if not bool(get('in_scope', True)):
        reasons.append('前件与后件未在同一台设备中出现，未进入检验范围')
    if count < minimum_count(valid_jobs, min_support):
        reasons.append('共同作业数未达最小支持度')
    if support + EPS < min_support:
        reasons.append('支持度低于正式阈值')
    if confidence + EPS < min_confidence:
        reasons.append('置信度低于正式阈值')
    if lift + EPS < min_lift:
        reasons.append('提升度低于正式阈值')
    if q_value is None:
        reasons.append('缺少精确检验结果')
    elif q_value > alpha + EPS:
        reasons.append('多重校正后 q=%.4g 高于显著性阈值 %.4g' % (q_value, alpha))
    return reasons


def validate_thresholds(min_support, min_confidence, min_lift, alpha):
    if not 0 < min_support <= 1:
        raise ValueError('最小支持度必须在 (0,1] 内')
    if not 0 < min_confidence <= 1:
        raise ValueError('最小置信度必须在 (0,1] 内')
    if not 0 <= min_lift <= 50:
        raise ValueError('最小提升度必须在 [0,50] 内')
    if not 0 < alpha <= 1:
        raise ValueError('显著性阈值必须在 (0,1] 内')


def _family_size(universe_size):
    return sum(
        math.comb(universe_size, size) * (2 ** size - 2)
        for size in range(2, MAX_ITEMS + 1)
    )


def analyze(rows, min_support=.15, min_confidence=.6, min_lift=1, alpha=.05):
    """返回 (result, candidates)。

    result 是批次摘要（含达标规则 rules）；candidates 是全部候选，
    含未达标、零共现与未进入检验范围的规则，便于按最新阈值复核。
    校正范围只由冻结样本决定，与阈值无关。
    """
    validate_thresholds(min_support, min_confidence, min_lift, alpha)
    groups, excluded, excluded_by_equipment = _prepare(rows)
    valid_jobs = sum(len(transactions) for _, transactions in groups)
    if valid_jobs < 2:
        raise ValueError('至少需要2条含备件的有效维保作业')

    universe = sorted(set().union(
        *(transaction for _, transactions in groups for transaction in transactions)
    ))
    minimum = minimum_count(valid_jobs, min_support)
    cache = {}
    candidates = []
    for antecedent_tuple, consequent_tuple, signature in _candidate_rules(universe):
        antecedent = frozenset(antecedent_tuple)
        consequent = frozenset(consequent_tuple)
        p_value, count, expected, per_equipment, in_scope = _exact_stratified(
            groups, antecedent, consequent, cache
        )
        a_total = sum(
            antecedent <= transaction
            for _, transactions in groups
            for transaction in transactions
        )
        b_total = sum(
            consequent <= transaction
            for _, transactions in groups
            for transaction in transactions
        )
        support = count / valid_jobs
        confidence = count / a_total if a_total else 0.0
        lift = confidence / (b_total / valid_jobs) if b_total else 0.0
        candidates.append({
            'signature': signature,
            'antecedent': list(antecedent_tuple),
            'consequent': list(consequent_tuple),
            'count': count,
            'support': support,
            'confidence': confidence,
            'lift': lift,
            'expected': expected,
            'p_value': p_value,
            'q_value': 1.0,
            'in_scope': in_scope,
            'per_equipment': per_equipment,
        })

    # 校正范围在枚举时已固定：同设备两侧都见过即入校正，零共现也不剔除。
    scoped = [candidate for candidate in candidates if candidate['in_scope']]
    _bh_qvalues(scoped)

    qualified = []
    for candidate in candidates:
        reasons = rule_reasons(
            candidate, valid_jobs, min_support, min_confidence, min_lift, alpha
        )
        candidate['qualified'] = not reasons
        candidate['reasons'] = reasons
        if candidate['qualified']:
            qualified.append(candidate)

    itemsets = []
    for size in range(1, MAX_ITEMS + 1):
        for combo in combinations(universe, size):
            itemset = frozenset(combo)
            count = _counts(groups, itemset)
            itemsets.append({
                'items': list(combo),
                'count': count,
                'support': count / valid_jobs,
            })

    result = {
        'valid_jobs': valid_jobs,
        'excluded': excluded,
        'minimum_count': minimum,
        'alpha': alpha,
        'equipment_counts': [
            {
                'equipment': equipment,
                'jobs': len(transactions),
                'excluded': excluded_by_equipment.get(equipment, 0),
            }
            for equipment, transactions in groups
        ],
        'universe': universe,
        'candidate_count': len(candidates),
        'scope_candidates': len(scoped),
        'family_estimate': _family_size(len(universe)),
        'qualified_count': len(qualified),
        'itemsets': itemsets,
        'frequent': [item for item in itemsets if item['count'] >= minimum],
        'rules': qualified,
        'version': VERSION,
        'checksum': hashlib.sha256(
            json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        'method': (
            '按设备固定前后件频数，零假设下各组共同出现次数服从超几何分布，'
            '卷积合并后取 P(X≥所见次数) 的单侧精确概率；对同一台设备中两侧都曾出现的'
            '全部候选（含零共现）做 Benjamini-Hochberg 多重校正，'
            '支持度/置信度/提升度/显著性阈值只筛选结果，不改变校正范围。'
            '候选为 A∪B 不超过3项的规则；关联不代表必须联换。'
        ),
    }
    return result, candidates


def mine(rows, min_support=.15, min_confidence=.6, min_lift=1, alpha=.05):
    """保留原调用方式：返回含达标规则的结果字典。"""
    result, _ = analyze(rows, min_support, min_confidence, min_lift, alpha)
    return result
