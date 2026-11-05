"""Run the 21 environment-specific regressions using installed server images.

Requires Docker, NVIDIA runtime and the private HDR fixture. Never loads the
production .env or mounts writable production data. Temporary PostgreSQL has
its own internal network, no published ports and a tmpfs data directory.
"""
from __future__ import annotations

import argparse
import json
import hashlib
from pathlib import Path
import stat
import subprocess
import time
import uuid


def command(args, *, timeout=120):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout).stdout.strip()


RUNNER = """
import json,os,unittest
from pathlib import Path
suite=unittest.defaultTestLoader.loadTestsFromNames(json.loads(os.environ['EXTRA_TEST_NAMES']))
def ids(suite):
 for item in suite:
  if isinstance(item,unittest.TestSuite):yield from ids(item)
  else:yield item.id()
cases=list(ids(suite))
result=unittest.TextTestRunner(verbosity=2).run(suite)
report={'run':result.testsRun,'cases':cases,'skipped':[{'id':t.id(),'reason':r} for t,r in result.skipped],
        'failures':[t.id() for t,_ in result.failures], 'errors':[t.id() for t,_ in result.errors]}
print('EXTRA_TEST_RESULT='+json.dumps(report),flush=True)
raise SystemExit(0 if result.wasSuccessful() and not result.skipped and result.testsRun==int(os.environ['EXTRA_TEST_COUNT']) else 1)
"""


def run(source: Path, report_path: Path, fixture: Path):
    bot_image=command(['docker','inspect','-f','{{.Image}}','tiktok-bot'])
    asr_image=command(['docker','inspect','-f','{{.Image}}','iglogin'])
    pg_image=command(['docker','inspect','-f','{{.Image}}','postgres-tiktok'])
    for name in ('bot','scripts','tests','proxy'):
        if not (source/name).is_dir():
            raise RuntimeError(f'Missing source directory: {name}')
    if not fixture.is_file():
        raise RuntimeError('Missing private HDR fixture; real GPU test cannot be skipped')
    prefix='adl-extra-tests-'+uuid.uuid4().hex[:12]
    network=prefix+'-net'
    pg=prefix+'-db'
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report={'bot_image':bot_image,'asr_image':asr_image,'postgres_image':pg_image,
            'components':{},'production_data_mounted':False,'external_network':False}
    tested_files=['tests/test_dashboard_access.py','tests/test_dashboard_data.py',
                  'tests/test_operator_integration.py','tests/test_ig_audio_transcribe.py',
                  'tests/test_nvenc_quality.py','scripts/ig_audio_transcribe.py',
                  'bot/dashboard.py','bot/dashboard_data.py','bot/operator.py',
                  'bot/broadcasts.py','bot/downloader.py','scripts/server-extra-tests.py']
    report['source_sha256']={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in tested_files}
    resources=[]
    gpu_modes={}
    for name in ('nvidia0','nvidiactl','nvidia-uvm','nvidia-uvm-tools'):
        path=Path('/dev')/name
        info=path.stat()
        if not stat.S_ISCHR(info.st_mode):raise RuntimeError(f'Not a GPU device: {path}')
        gpu_modes[path]=stat.S_IMODE(info.st_mode)
    started=time.monotonic()
    try:
        command(['docker','network','create','--internal','--label','adl.extra-tests='+prefix,network])
        resources.append(('network',network))
        resources.append(('container',pg))
        command(['docker','run','-d','--name',pg,'--network',network,'--network-alias','test-db',
                 '--label','adl.extra-tests='+prefix,'--memory','256m','--cpus','.5','--pids-limit','128',
                 '--tmpfs','/var/lib/postgresql/data:rw,size=128m',
                 '-e','POSTGRES_USER=ci','-e','POSTGRES_PASSWORD=ci','-e','POSTGRES_DB=ci',pg_image])
        for attempt in range(60):
            status=subprocess.run(['docker','exec',pg,'pg_isready','-U','ci','-d','ci'],capture_output=True,timeout=5)
            if status.returncode==0:break
            time.sleep(.5)
        else:raise RuntimeError('Isolated PostgreSQL did not become ready')

        def component(label,names,count,image,*,gpu=False,db=False):
            name=prefix+'-'+label
            args=['docker','run','--name',name,'--label','adl.extra-tests='+prefix,
                  '--memory','1024m','--cpus','.5','--pids-limit','256',
                  '--network',network if db else 'none','-w','/src','--entrypoint','python',
                  '-e','PYTHON_DOTENV_DISABLED=1','-e','EXTRA_TEST_NAMES='+json.dumps(names),
                  '-e','EXTRA_TEST_COUNT='+str(count)]
            directories=('tests','scripts') if label=='asr' else ('bot','scripts','tests','proxy')
            for directory in directories:
                args.extend(['-v',f'{source/directory}:/src/{directory}:ro'])
            if db:
                for value in ('BOT_TOKEN=123456:TEST_ONLY','ADMIN_ID=101','DB_HOST=test-db',
                              'DB_USER=ci','DB_PASSWORD=ci','DB_NAME=ci','OPERATOR_DB_INTEGRATION=1',
                              'OPERATOR_TEST_DSN=postgresql://ci:ci@test-db:5432/ci',
                              'DASHBOARD_TEST_DSN=postgresql://ci:ci@test-db:5432/ci'):
                    args.extend(['-e',value])
            if gpu:
                args.extend(['--runtime','nvidia','--group-add','104',
                             '-e','NVIDIA_VISIBLE_DEVICES=all',
                             '-e','NVIDIA_DRIVER_CAPABILITIES=compute,video,utility',
                             '-e','NVENC_QUALITY_FIXTURE=/qa/hdr.mp4','-v',f'{fixture}:/qa/hdr.mp4:ro'])
            args.extend([image,'-c',RUNNER])
            resources.append(('container',name))
            result=subprocess.run(args,capture_output=True,text=True,timeout=600)
            log=result.stdout+result.stderr
            report_path.with_name(report_path.stem+'-'+label+'.log').write_text(log,encoding='utf-8')
            summaries=[line.split('=',1)[1] for line in result.stdout.splitlines() if line.startswith('EXTRA_TEST_RESULT=')]
            summary=json.loads(summaries[-1]) if summaries else {'error':'missing test report'}
            report['components'][label]=summary
            print(json.dumps({'component':label,**{k:v for k,v in summary.items() if k!='cases'}}),flush=True)
            if result.returncode or summary.get('run')!=count or summary.get('skipped'):
                raise RuntimeError(f'{label} tests failed; see component log')

        component('postgres',[
            'tests.test_dashboard_access.DashboardAccessPostgresTests',
            'tests.test_dashboard_data.DashboardPostgresTests',
            'tests.test_operator_integration.OperatorDatabaseIntegration'],19,bot_image,db=True)
        component('asr',['tests.test_ig_audio_transcribe.AudioTranscribeTests.test_uses_only_local_tiny_model_and_clean_json_stdout'],1,asr_image)
        component('nvidia',['tests.test_nvenc_quality.NvidiaQualityTests'],1,bot_image,gpu=True)
        report['success']=True
    finally:
        # Only our unpredictable resource names are removed; no volumes,
        # production containers, Docker daemon or running services are touched.
        errors=[]
        for kind,name in reversed(resources):
            args=['docker','rm','-f',name] if kind=='container' else ['docker','network','rm',name]
            result=subprocess.run(args,capture_output=True,text=True,timeout=60)
            if result.returncode and 'No such' not in result.stderr:errors.append(name)
        for path,mode in gpu_modes.items():
            path.chmod(mode)
        report['cleanup_errors']=errors
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if errors:raise RuntimeError('Temporary test resource cleanup failed')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path.cwd())
    parser.add_argument('--report',type=Path,default=Path('data/audit/server-extra-tests.json'))
    parser.add_argument('--fixture',type=Path,default=Path('data/qa-fixtures/nvenc-hdr-motion.mp4'))
    args=parser.parse_args()
    run(args.source.resolve(),args.report.resolve(),args.fixture.resolve())
