import os,time,json,logging
from pathlib import Path
import redis
from src.config.database import Session
from src.models.entities import MiningRun
from src.services import evidence
from src.services.analysis import analyze
r=redis.Redis.from_url(os.environ['REDIS_URL'])
def recover():
 """重启后把未完成批次放回队列。

 样本在提交时已经冻结在 result.snapshot 里，恢复和重试都沿用原样本，
 不重新查询作业表，也不新增批次或关联规则。
 """
 with Session() as recovery:
  for pending in recovery.query(MiningRun).filter(MiningRun.state.in_(['queued','running'])).all():
   if not (pending.result or {}).get('snapshot'):
    pending.state='failed';pending.error='批次缺少冻结样本，无法恢复，请新建挖掘'
   else:
    pending.state='queued';r.rpush('affinity:runs',pending.id)
  recovery.commit()
def write_artifact(run):
 try:
  Path('/artifacts').mkdir(parents=True,exist_ok=True)
  Path('/artifacts/'+str(run.id)+'.json').write_text(json.dumps(run.result,ensure_ascii=False))
 except OSError:logging.warning('artifact write failed for run %s',run.id)

def run_one(run_id):
 with Session() as s:
  # 只认领 queued：重复投递或运行中的批次不会被第二个 worker 再算一遍。
  run=s.get(MiningRun,run_id)
  if not run or run.state!='queued':return
  # 行锁保证同一批次的认领与阈值调整串行。
  run=evidence.lock_run(s,run_id)
  if run.state!='queued':return
  snapshot=evidence.snapshot_of(run)
  if not snapshot:
   run.state='failed';run.error='批次缺少冻结样本，无法恢复，请新建挖掘';s.commit();return
  run.state='running';s.commit()
  try:
   thresholds=evidence.thresholds_of(run)
   result,candidates=analyze(
    snapshot,thresholds['min_support'],thresholds['min_confidence'],
    thresholds['min_lift'],thresholds['alpha'])
   evidence.persist_candidates(s,run,result,candidates)
   run.state='done';run.error=''
   write_artifact(run)
  except Exception as e:run.state='failed';run.error=str(e)
  s.commit()
def main():
 recover()
 while True:
  Path('/tmp/heartbeat').touch()
  try:
   item=r.blpop('affinity:runs',timeout=3)
   if not item:continue
   run_one(int(item[1]))
  except Exception:logging.exception('worker');time.sleep(2)
if __name__=='__main__':main()
