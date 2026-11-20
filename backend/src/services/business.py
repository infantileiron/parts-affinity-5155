from datetime import datetime, timezone
from src.models.entities import Audit
def audit(s,u,action):
 s.add(Audit(actor=u.email,action=action,created=datetime.now(timezone.utc).isoformat()));s.commit()
def dump(obj):return {c.name:getattr(obj,c.name) for c in obj.__table__.columns if c.name!='password'}
def public_run(run):
 """批次响应只带摘要；冻结快照、逐条候选与项集明细不随列表返回。"""
 data=dump(run)
 result=data.get('result')
 if isinstance(result,dict):
  result={key:value for key,value in result.items()
          if key not in ('snapshot','candidates','itemsets','frequent','universe','rules')}
  data['result']=result
 return data
