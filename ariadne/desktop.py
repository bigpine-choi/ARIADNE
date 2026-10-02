"""Portable Windows launch and owner-only schedules; no administrator required."""
import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import webbrowser
import xml.etree.ElementTree as ET

from .local_settings import DEFAULTS, load_settings, validate_settings

TASKS = {'omen': 'ARIADNE Weekly Report', 'company-relay': 'ARIADNE Shared Folder Relay'}


def location_id(directory):
    return hashlib.sha256(os.path.normcase(str(Path(directory).resolve())).encode()).hexdigest()[:24]


def initial_settings(role, directory, profile):
    if role not in TASKS: raise ValueError('Choose omen or company-relay')
    root = Path(directory).resolve()
    result = {**DEFAULTS, 'device_role': role}
    if profile.get('company_web_url'):
        result.update(handoff_transport='web',handoff_web_url=profile['company_web_url'])
    if role == 'company-relay':
        result.update(folder=profile.get('company_folder', ''), weekly_archive_enabled=True)
    else:
        result.update(folder=str(root/'shared-meetings'), mailbox=profile.get('mailbox', ''),
            teams_chats=profile.get('teams_chats', []), hiworks_company=profile.get('hiworks_company', ''),
            report_template=str(root/'shared-meetings'/profile.get('template_relative', 'template.xlsx')),
            model=profile.get('model', ''), handoff_required=True)
    return validate_settings(result)


def seed_settings(directory, role, profile):
    directory=Path(directory).resolve(); directory.mkdir(parents=True, exist_ok=True)
    path=directory/'automation.json'
    if path.exists():
        settings=load_settings(path)
        if settings['device_role']!=role:
            raise ValueError('이 데이터 폴더는 다른 PC 역할로 설정되어 있습니다. 별도 데이터 폴더를 사용하세요.')
        return settings
    settings=initial_settings(role,directory,profile)
    # First run only. Never overwrite a key or a user's connection settings.
    with path.open('x',encoding='utf-8') as handle:json.dump(settings,handle,ensure_ascii=False,indent=2)
    return settings


def schedule_xml(role, sid, pythonw, project, directory, *, at=None, ui=False):
    if role not in TASKS: raise ValueError('Unsupported role')
    directory=Path(directory).resolve()
    namespace='http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('',namespace)
    def tag(name):return '{'+namespace+'}'+name
    task=ET.Element(tag('Task'),version='1.2')
    def child(parent,name,text=None,**attrs):
        result=ET.SubElement(parent,tag(name),attrs)
        if text is not None:result.text=str(text)
        return result
    registration=child(task,'RegistrationInfo');child(registration,'Description',
        'ARIADNE 로그인된 본인 계정 · 회사 공유폴더 전달' if role=='company-relay' else 'ARIADNE 오멘 수집·주간회의 자료')
    triggers=child(task,'Triggers')
    base=(at or datetime.now()).replace(hour=0,minute=0,second=0,microsecond=0,tzinfo=None)
    if ui:
        trigger=child(triggers,'LogonTrigger');child(trigger,'Enabled','true');child(trigger,'UserId',sid)
    else:
        moments=[(h,0,False) for h in range(9,19)]
        if role=='omen':moments.append((16,15,True))
        for hour,minute,friday in moments:
            trigger=child(triggers,'CalendarTrigger')
            child(trigger,'StartBoundary',(base+timedelta(hours=hour,minutes=minute)).isoformat())
            child(trigger,'Enabled','true')
            weekly=child(trigger,'ScheduleByWeek');child(weekly,'WeeksInterval','1');days=child(weekly,'DaysOfWeek')
            for day in (['Friday'] if friday else ['Monday','Tuesday','Wednesday','Thursday','Friday']):child(days,day)
    principals=child(task,'Principals');principal=child(principals,'Principal',id='Owner')
    child(principal,'UserId',sid);child(principal,'LogonType','InteractiveToken');child(principal,'RunLevel','LeastPrivilege')
    settings=child(task,'Settings')
    for name,value in [('MultipleInstancesPolicy','IgnoreNew' if ui else 'Queue'),
        ('DisallowStartIfOnBatteries','false'),('StopIfGoingOnBatteries','false'),('StartWhenAvailable','false'),
        ('Enabled','true'),('WakeToRun','true'),('ExecutionTimeLimit','PT0S' if ui else 'PT25M')]:child(settings,name,value)
    actions=child(task,'Actions',Context='Owner');action=child(actions,'Exec')
    child(action,'Command',str(pythonw))
    arguments=(['-m','ariadne.desktop','--data-dir',str(directory),'background'] if ui else
        ['-m','ariadne.automation_cli','--settings',str(directory/'automation.json'),'--data-dir',str(directory),'tick'])
    child(action,'Arguments',subprocess.list2cmdline(arguments));child(action,'WorkingDirectory',str(project))
    return ET.tostring(task,encoding='unicode')


def register_schedule(directory, role):
    if os.name!='nt':raise ValueError('예약 등록은 Windows에서 실행하세요.')
    if datetime.now().astimezone().utcoffset()!=timedelta(hours=9):raise ValueError('PC 시간대를 서울로 설정하세요.')
    directory=Path(directory).resolve();settings=load_settings(directory/'automation.json')
    if settings['device_role']!=role:raise ValueError('PC 역할과 예약 역할이 다릅니다.')
    if settings.get('handoff_transport')=='web':
        from .cloud_handoff import connection_status,validate_url
        validate_url(settings['handoff_web_url'])
        if connection_status(directory)['status']!='connected':
            raise ValueError('먼저 회사 웹 OneDrive 연결을 열어 로그인 연결을 확인하세요.')
    elif not settings['handoff_dir'] or not Path(settings['handoff_dir']).is_dir():
        raise ValueError('ARIADNE 연결 설정에 회사 OneDrive의 로컬 ARIADNE 폴더를 저장하세요.')
    from .handoff import read_key
    read_key(directory)
    if role=='company-relay' and not Path(settings['folder']).is_dir():raise ValueError('회사 공유폴더에 연결하세요.')
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():raise ValueError('예약 실행에 pythonw.exe가 필요합니다.')
    project=Path(__file__).resolve().parent.parent
    output=subprocess.check_output(['whoami','/user','/fo','csv','/nh'],text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    sid=next(csv.reader([output.strip()]))[1]
    for ui in [False,True]:
        name='ARIADNE Local UI' if ui else TASKS[role]
        with tempfile.NamedTemporaryFile(suffix='.xml',delete=False,mode='w',encoding='utf-16') as handle:
            path=Path(handle.name);handle.write(schedule_xml(role,sid,pythonw,project,directory,ui=ui))
        try:subprocess.run(['schtasks','/Create','/TN',name,'/XML',str(path),'/F'],check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        finally:path.unlink(missing_ok=True)


def schedule_status(role):
    if os.name!='nt' or role not in TASKS:return {'registered':False,'enabled':False}
    try:
        result=subprocess.run(['schtasks','/Query','/TN',TASKS[role],'/XML'],capture_output=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:return {'registered':False,'enabled':False}
        data=result.stdout
        if b'\x00' in data:data=data.decode('utf-16')
        else:
            try:data=data.decode('utf-8')
            except UnicodeDecodeError:data=data.decode('mbcs',errors='replace')
        root=ET.fromstring(data);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        enabled=root.find('t:Settings/t:Enabled',ns)
        return {'registered':True,'enabled':enabled is None or enabled.text=='true',
            'triggers':len(root.findall('t:Triggers/t:CalendarTrigger',ns))}
    except Exception:return {'registered':False,'enabled':False,'message':'Windows 예약 상태를 확인하지 못했습니다.'}


def launch(directory, *, show=True):
    directory=Path(directory).resolve();url='http://127.0.0.1:8766'
    def probe():
        with urlopen(url+'/api/health',timeout=2) as response:result=json.load(response)
        if result.get('dataLocationId')!=location_id(directory):
            raise ValueError('다른 ARIADNE 데이터 폴더가 이미 실행 중입니다. 해당 화면을 종료한 뒤 다시 시작하세요.')
        return True
    try:running=probe()
    except (OSError, TimeoutError):running=False
    if not running:
        pythonw=Path(sys.executable).with_name('pythonw.exe')
        if not pythonw.is_file():raise ValueError('pythonw.exe가 필요합니다.')
        environment={**os.environ,'ARIADNE_DATA_DIR':str(directory)}
        logs=directory/'logs';logs.mkdir(parents=True,exist_ok=True)
        with (logs/'server.log').open('ab') as log:
            subprocess.Popen([str(pythonw),'-m','ariadne','serve','--port','8766'],
                cwd=Path(__file__).resolve().parent.parent,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+15
        while True:
            try:probe();break
            except (OSError,TimeoutError):
                if time.monotonic()>=deadline:raise ValueError('화면 시작에 실패했습니다. 데이터 폴더의 logs/server.log를 확인하세요.') from None
                time.sleep(.3)
    if show:webbrowser.open(url+'/#settings')


def main(argv=None):
    parser=argparse.ArgumentParser(description='ARIADNE 운영 PC 설정')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--profile',type=Path)
    parser.add_argument('command',choices=['omen','company-relay','background','register','stop'])
    args=parser.parse_args(argv)
    role=args.command if args.command in TASKS else None
    from .config import data_directory
    directory=args.data_dir or (Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local'))/'ARIADNE-SharedRelay' if role=='company-relay' else data_directory())
    if role:
        profile=json.loads(args.profile.read_text(encoding='utf-8-sig')) if args.profile else {}
        seed_settings(directory,role,profile);launch(directory)
    elif args.command=='background':launch(directory,show=False)
    else:
        role=load_settings(directory/'automation.json')['device_role']
        if role not in TASKS:raise ValueError('먼저 운영 PC를 선택하세요.')
        if args.command=='register':register_schedule(directory,role);print('예약 등록 완료. 실제 작동 스위치는 ARIADNE 연결 설정에서 관리합니다.')
        else:
            for name in [TASKS[role],'ARIADNE Local UI']:
                subprocess.run(['schtasks','/Change','/TN',name,'/DISABLE'],check=True)
            print('예약을 중지했습니다.')


if __name__=='__main__':
    try:main()
    except Exception as error:
        if os.name=='nt' and sys.stdout is None:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(error),'ARIADNE 설정 확인',0x10)
        else:print(str(error),file=sys.stderr)
        raise SystemExit(1)
