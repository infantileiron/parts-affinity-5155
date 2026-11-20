"""候选家族、正式阈值与判定依据。

候选规则在挖掘完成时一次性写入 association_rules；之后不论试看、收紧
正式阈值还是起草备件包，都只在这同一家族上按阈值重新判定，不重新采样，
也不改变多重校正范围（q 值固定）。
"""
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select
from src.models.entities import MiningRun, AssociationRule
from src.services.analysis import (
    VERSION,
    analyze,
    candidate_signatures,
    minimum_count,
    rule_reasons,
    validate_thresholds,
)

RULE_FIELDS = (
    'signature', 'antecedent', 'consequent', 'count', 'support', 'confidence',
    'lift', 'expected', 'p_value', 'q_value', 'in_scope', 'per_equipment',
)


def now():
    return datetime.now(timezone.utc).isoformat()


def thresholds_of(run):
    return {
        'min_support': run.min_support,
        'min_confidence': run.min_confidence,
        'min_lift': run.min_lift,
        'alpha': run.alpha if run.alpha is not None else .05,
    }


def snapshot_of(run):
    return (run.result or {}).get('snapshot') or []


def lock_run(s, run_id):
    """锁定批次行，串行化正式阈值调整与起草判定。"""
    run = s.execute(
        select(MiningRun)
        .where(MiningRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(404, '挖掘批次不存在')
    return run


def stored_candidates(s, run_id):
    return (
        s.query(AssociationRule)
        .filter_by(run_id=run_id)
        .order_by(AssociationRule.id)
        .all()
    )


def locked_candidates(s, run_id):
    """在已持有批次行锁的事务里读取候选。

    MySQL 默认 REPEATABLE READ 下普通 SELECT 会沿用事务开始时的快照，
    maybe 看到并发阈值调整前的旧判定；这里用锁定读并强制刷新已加载对象。
    """
    return (
        s.execute(
            select(AssociationRule)
            .where(AssociationRule.run_id == run_id)
            .order_by(AssociationRule.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )


def family_signatures(base):
    """由冻结样本的备件全集重建候选签名，用于识别历史遗留规则行。"""
    return candidate_signatures(base.get('universe') or [])


def needs_upgrade(run, rows):
    """旧版本批次（缺精确检验列或候选家族）需要按冻结样本补齐。"""
    base = run.result or {}
    if base.get('version') != VERSION:
        return True
    if not base.get('universe') or 'scope_candidates' not in base:
        return True
    family = family_signatures(base)
    return any(row.q_value is None for row in rows if row.signature in family)


def candidate_dict(row):
    return {field: getattr(row, field) for field in RULE_FIELDS}


def annotate(candidates, valid_jobs, thresholds):
    """按阈值标记每条候选，返回达标子集；不修改 q 值。"""
    qualified = []
    for candidate in candidates:
        reasons = rule_reasons(
            candidate, valid_jobs, thresholds['min_support'],
            thresholds['min_confidence'], thresholds['min_lift'], thresholds['alpha'],
        )
        candidate['qualified'] = not reasons
        candidate['reasons'] = reasons
        if candidate['qualified']:
            qualified.append(candidate)
    return qualified


def payload(candidate, row_id, run_id):
    data = {field: candidate.get(field) for field in RULE_FIELDS}
    data.update({
        'id': row_id,
        'run_id': run_id,
        'qualified': bool(candidate.get('qualified')),
        'reasons': candidate.get('reasons') or [],
    })
    return data


def _summary(run, base, thresholds, candidates, valid_jobs):
    minimum = minimum_count(valid_jobs, thresholds['min_support'])
    return {
        'run_id': run.id,
        'state': run.state,
        'name': run.name,
        'equipment': run.equipment,
        'valid_jobs': valid_jobs,
        'excluded': base.get('excluded', 0),
        'equipment_counts': base.get('equipment_counts', []),
        'universe': base.get('universe', []),
        'candidate_count': len(candidates),
        'scope_candidates': sum(1 for c in candidates if c.get('in_scope')),
        'family_estimate': base.get('family_estimate', 0),
        'minimum_count': minimum,
        'qualified_count': sum(1 for c in candidates if c.get('qualified')),
        'itemsets': base.get('itemsets', []),
        'frequent': [
            item for item in base.get('itemsets', []) if item['count'] >= minimum
        ],
        'version': base.get('version', VERSION),
        'checksum': base.get('checksum', ''),
        'method': base.get('method', ''),
        'thresholds': thresholds,
        'formal_thresholds': thresholds_of(run),
        'threshold_revision': (run.result or {}).get('threshold_revision', 0),
        'threshold_history': (run.result or {}).get('threshold_history', []),
    }


def _pair(rows, candidates):
    """把候选与数据库行按签名配对，返回 (payloads, 遗留行)。"""
    by_signature = {row.signature: row for row in rows}
    payloads = []
    matched = set()
    for candidate in candidates:
        row = by_signature.get(candidate['signature'])
        if row is None:
            continue
        matched.add(candidate['signature'])
        payloads.append(payload(candidate, row.id, row.run_id))
    orphans = [row for signature, row in by_signature.items() if signature not in matched]
    return payloads, orphans


def view(s, run, thresholds=None, include_all=False, preview=None, allow_compute=True):
    """某批次在给定阈值下的视图；试看不写库。

    allow_compute=False 时旧批次不会触发精确检验重算（观察员只读路径），
    只按已存字段展示并标注待复核。
    """
    thresholds = thresholds or thresholds_of(run)
    validate_thresholds(
        thresholds['min_support'], thresholds['min_confidence'],
        thresholds['min_lift'], thresholds['alpha'],
    )
    if run.state != 'done':
        return {
            'run_id': run.id, 'state': run.state, 'name': run.name,
            'equipment': run.equipment, 'rules': [], 'all_rules': [],
            'candidate_count': 0, 'scope_candidates': 0, 'qualified_count': 0,
            'thresholds': thresholds, 'formal_thresholds': thresholds_of(run),
            'error': run.error or '批次尚未完成计算', 'preview': bool(preview),
        }
    rows = stored_candidates(s, run.id)
    base = run.result or {}
    legacy = needs_upgrade(run, rows)
    if legacy and not allow_compute:
        candidates = [candidate_dict(row) for row in rows]
        valid_jobs = int(base.get('valid_jobs') or 0)
        annotate(candidates, valid_jobs, thresholds)
        payloads = [payload(candidate, row.id, row.run_id) for candidate, row in zip(candidates, rows)]
        data = _summary(run, base, thresholds, candidates, valid_jobs)
        data.update({
            'rules': [item for item in payloads if item['qualified']],
            'all_rules': payloads if include_all else [],
            'preview': bool(preview),
            'legacy_upgrade_required': True,
            'legacy_notice': '旧批次尚未按分层精确检验复核，请由分析师调整阈值或起草时刷新判定。',
            'legacy_rule_ids': [row.id for row in rows],
        })
        return data
    if legacy:
        snapshot = snapshot_of(run)
        if not snapshot:
            raise HTTPException(409, '批次缺少冻结样本，无法核对精确检验，请新建挖掘')
        base, candidates = analyze(
            snapshot, thresholds['min_support'], thresholds['min_confidence'],
            thresholds['min_lift'], thresholds['alpha'],
        )
    else:
        family = family_signatures(base)
        candidates = [
            candidate_dict(row) for row in rows if row.signature in family
        ]
    valid_jobs = int(base.get('valid_jobs') or 0)
    annotate(candidates, valid_jobs, thresholds)
    payloads, orphans = _pair(rows, candidates)
    data = _summary(run, base, thresholds, candidates, valid_jobs)
    data.update({
        'rules': [item for item in payloads if item['qualified']],
        'all_rules': payloads if include_all else [],
        'preview': bool(preview),
        'legacy_upgrade_required': legacy,
        'legacy_rule_ids': [row.id for row in orphans],
    })
    return data


def _write_candidates(s, run, result, candidates, snapshot):
    """把候选家族写入该批次；已有签名沿用原 id，遗留行保留不动。"""
    rows = {row.signature: row for row in stored_candidates(s, run.id)}
    for candidate in candidates:
        row = rows.get(candidate['signature'])
        if row is None:
            row = AssociationRule(run_id=run.id, signature=candidate['signature'])
            s.add(row)
            rows[candidate['signature']] = row
        for field in RULE_FIELDS:
            setattr(row, field, candidate[field])
        row.qualified = candidate.get('qualified', False)
        row.reasons = candidate.get('reasons') or []
    previous = run.result or {}
    base = {key: value for key, value in result.items() if key != 'rules'}
    run.result = {
        **base,
        'snapshot': snapshot,
        'thresholds': thresholds_of(run),
        'threshold_revision': previous.get('threshold_revision', 0),
        'threshold_history': previous.get('threshold_history', []),
    }
    s.flush()
    return run.result


def persist_candidates(s, run, result, candidates):
    """worker 用：写入完整候选家族，冻结样本原样保留。"""
    return _write_candidates(s, run, result, candidates, snapshot_of(run))


def _upgrade_locked(s, run):
    """在行锁内用冻结样本补齐旧批次。"""
    snapshot = snapshot_of(run)
    if not snapshot:
        raise HTTPException(409, '批次缺少冻结样本，无法核对精确检验，请新建挖掘')
    thresholds = thresholds_of(run)
    result, candidates = analyze(
        snapshot, thresholds['min_support'], thresholds['min_confidence'],
        thresholds['min_lift'], thresholds['alpha'],
    )
    base = _write_candidates(s, run, result, candidates, snapshot)
    return base, candidates


def commit_thresholds(s, run_id, thresholds, actor):
    """在行锁内调整正式阈值；只重新判定，不扩缩校正家族。"""
    validate_thresholds(
        thresholds['min_support'], thresholds['min_confidence'],
        thresholds['min_lift'], thresholds['alpha'],
    )
    run = lock_run(s, run_id)
    if run.state != 'done':
        raise HTTPException(409, '仅已完成的挖掘批次可以调整正式阈值')
    rows = locked_candidates(s, run.id)
    if needs_upgrade(run, rows):
        base, candidates = _upgrade_locked(s, run)
        rows = locked_candidates(s, run.id)
    else:
        base = dict(run.result or {})
        family = family_signatures(base)
        candidates = [candidate_dict(row) for row in rows if row.signature in family]
    valid_jobs = int(base.get('valid_jobs') or 0)
    annotate(candidates, valid_jobs, thresholds)
    rows_by_signature = {row.signature: row for row in rows}
    for candidate in candidates:
        row = rows_by_signature[candidate['signature']]
        row.qualified = candidate['qualified']
        row.reasons = candidate['reasons']
    revision = int((run.result or {}).get('threshold_revision', 0)) + 1
    history = list((run.result or {}).get('threshold_history') or [])
    history.append({
        'revision': revision,
        'thresholds': thresholds,
        'decided_at': now(),
        'decided_by': actor.email,
    })
    run.min_support = thresholds['min_support']
    run.min_confidence = thresholds['min_confidence']
    run.min_lift = thresholds['min_lift']
    run.alpha = thresholds['alpha']
    base = dict(run.result or {})
    base.update({
        'version': base.get('version', VERSION),
        'thresholds': thresholds,
        'threshold_revision': revision,
        'threshold_history': history[-20:],
        'minimum_count': minimum_count(valid_jobs, thresholds['min_support']),
        'alpha': thresholds['alpha'],
        'qualified_count': sum(1 for c in candidates if c['qualified']),
        'rules': [c for c in candidates if c['qualified']],
    })
    run.result = base
    s.commit()
    return view(s, run, thresholds)


def evidence_for_kit(s, rule_id, actor):
    """起草前在批次行锁内复判；返回规则、批次与判定依据。"""
    rule = s.get(AssociationRule, rule_id)
    if not rule:
        raise HTTPException(404, '来源规则不存在')
    run = lock_run(s, rule.run_id)
    if run.state != 'done':
        raise HTTPException(409, '来源规则尚未完成')
    # 行锁之后用锁定读刷新规则与候选，避免用到并发阈值调整前的旧判定。
    thresholds = thresholds_of(run)
    rows = locked_candidates(s, run.id)
    by_id = {row.id: row for row in rows}
    rule = by_id.get(rule_id)
    if rule is None:
        rule = s.get(AssociationRule, rule_id, populate_existing=True)
    if rule is None:
        raise HTTPException(409, '来源规则不在本批次的候选家族内')
    if needs_upgrade(run, rows):
        base, candidates = _upgrade_locked(s, run)
        rows = locked_candidates(s, run.id)
        rule = next((row for row in rows if row.id == rule_id), None)
        if rule is None:
            raise HTTPException(409, '来源规则不在本批次的候选家族内')
    else:
        base = run.result or {}
        family = family_signatures(base)
        candidates = [candidate_dict(row) for row in rows if row.signature in family]
    candidate = next(
        (item for item in candidates if item['signature'] == rule.signature), None
    )
    if candidate is None:
        raise HTTPException(409, '来源规则不在本批次的候选家族内')
    valid_jobs = int(base.get('valid_jobs') or 0)
    reasons = rule_reasons(
        candidate, valid_jobs, thresholds['min_support'],
        thresholds['min_confidence'], thresholds['min_lift'], thresholds['alpha'],
    )
    if reasons:
        raise HTTPException(409, {
            'message': '规则未通过当前正式阈值，不能起草备件包',
            'reasons': reasons,
        })
    snapshot = snapshot_of(run)
    equipment_counts = base.get('equipment_counts') or [
        {'equipment': equipment, 'jobs': len(transactions), 'excluded': 0}
        for equipment, transactions in _groups(snapshot)
    ]
    record = {
        'decided_at': now(),
        'decided_by': actor.email,
        'run_id': run.id,
        'run_name': run.name,
        'run_equipment': run.equipment,
        'rule_id': rule.id,
        'rule_signature': candidate['signature'],
        'version': base.get('version', VERSION),
        'checksum': base.get('checksum', ''),
        'valid_jobs': valid_jobs,
        'minimum_count': minimum_count(valid_jobs, thresholds['min_support']),
        'thresholds': thresholds,
        'threshold_revision': base.get('threshold_revision', 0),
        'count': candidate['count'],
        'support': candidate['support'],
        'confidence': candidate['confidence'],
        'lift': candidate['lift'],
        'expected': candidate.get('expected'),
        'p_value': candidate.get('p_value'),
        'q_value': candidate.get('q_value'),
        'in_scope': candidate.get('in_scope'),
        'per_equipment': candidate.get('per_equipment'),
        'equipment_counts': equipment_counts,
        'qualified': True,
        'reasons': [],
    }
    return rule, run, record


def _groups(snapshot):
    from src.services.analysis import _prepare

    return _prepare(snapshot)[0]
