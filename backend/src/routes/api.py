import os,json
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from src.config.database import db
from src.models.entities import Job,MiningRun,Kit,User
from src.schemas.business import Login,JobInput,RunInput,ThresholdInput,KitInput,KitEdit
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump,public_run
from src.services.jobs import save_job
from src.controllers.business import ensure_run,ensure_kit
from src.services import evidence
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/jobs')
def jobs(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Job).order_by(Job.id).all()]
@router.post('/jobs')
def create_job(data:JobInput,u=Depends(editor),s=Depends(db)):
 r=save_job(s,data);audit(s,u,'录入作业 '+r['code']);return r
@router.put('/jobs/{i}')
def edit_job(i:int,data:JobInput,u=Depends(editor),s=Depends(db)):
 r=save_job(s,data,i);audit(s,u,'修订作业 '+str(i));return r
@router.get('/runs')
def runs(u=Depends(current),s=Depends(db)):return [public_run(r) for r in s.query(MiningRun).order_by(MiningRun.id.desc()).all()]
@router.post('/runs')
def run(data:RunInput,u=Depends(editor),s=Depends(db)):
 rows=[dump(r) for r in s.query(Job).order_by(Job.id).all() if data.equipment=='全部' or r.equipment==data.equipment]
 values=data.model_dump()
 r=MiningRun(**values,result={'snapshot':rows,'thresholds':{k:values[k] for k in ('min_support','min_confidence','min_lift','alpha')}})
 s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('affinity:runs',r.id)
 except redis.RedisError:
  r.state='failed';r.error='队列暂不可用，请恢复后新建挖掘';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交挖掘 '+str(r.id));return public_run(r)
@router.get('/runs/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return public_run(ensure_run(s,i))
def _thresholds(run,min_support,min_confidence,min_lift,alpha):
 values=evidence.thresholds_of(run)
 if min_support is not None:values['min_support']=min_support
 if min_confidence is not None:values['min_confidence']=min_confidence
 if min_lift is not None:values['min_lift']=min_lift
 if alpha is not None:values['alpha']=alpha
 try:evidence.validate_thresholds(**values)
 except ValueError as e:raise HTTPException(422,str(e))
 return values
@router.get('/runs/{i}/preview')
def preview(i:int,min_support:float|None=None,min_confidence:float|None=None,min_lift:float|None=None,alpha:float|None=None,u=Depends(editor),s=Depends(db)):
 """按临时阈值试看：只读，不改正式阈值，也不改变多重校正范围。"""
 r=ensure_run(s,i)
 return evidence.view(s,r,_thresholds(r,min_support,min_confidence,min_lift,alpha),preview=True)
@router.put('/runs/{i}/thresholds')
def set_thresholds(i:int,data:ThresholdInput,u=Depends(editor),s=Depends(db)):
 """收紧/放宽正式阈值；与起草并发时在批次行锁内串行判定。"""
 result=evidence.commit_thresholds(s,i,data.model_dump(),u)
 audit(s,u,'调整正式阈值 批次'+str(i))
 return result
@router.get('/rules')
def rules(run_id:int,u=Depends(current),s=Depends(db)):
 # 观察员只读展示，不触发旧批次重算
 r=ensure_run(s,run_id)
 return evidence.view(s,r,allow_compute=u.role!='viewer')['rules']
@router.get('/runs/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_run(s,i)
 if r.state!='done':raise HTTPException(409,'仅成功挖掘可以导出')
 data=evidence.view(s,r,include_all=True,allow_compute=u.role!='viewer')
 return Response(json.dumps(data,ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="parts-affinity-{i}.json"'})
@router.get('/kits')
def kits(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Kit).order_by(Kit.id.desc()).all()]
@router.post('/kits')
def kit(data:KitInput,u=Depends(editor),s=Depends(db)):
 rule,run,record=evidence.evidence_for_kit(s,data.rule_id,u)
 current=evidence.thresholds_of(run)
 if data.expected_thresholds:
  for key,value in data.expected_thresholds.model_dump().items():
   if abs(float(current[key])-float(value))>1e-12:
    raise HTTPException(409,'正式阈值已由他人调整，请刷新规则判定后重新起草')
 r=Kit(name=data.name,rule_id=rule.id,note=data.note,status='draft',
       parts=sorted(set(rule.antecedent+rule.consequent)),evidence=record)
 s.add(r);s.commit();audit(s,u,'起草备件包 '+str(r.id))
 return dump(r)
@router.put('/kits/{i}')
def edit_kit(i:int,data:KitEdit,u=Depends(editor),s=Depends(db)):
 r=ensure_kit(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核备件包不可覆盖，需新建方案')
 r.name=data.name;r.note=data.note;s.commit();audit(s,u,'修改备件包 '+str(i));return dump(r)
@router.post('/kits/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=ensure_kit(s,i)
 if r.status!='draft':raise HTTPException(409,'已签核方案不可重复签核')
 r.status='approved';s.commit();audit(s,u,'签核备件包 '+str(i));return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]
