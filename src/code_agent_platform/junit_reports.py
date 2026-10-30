"""Read bounded JUnit XML produced by Maven/Surefire in the task workspace."""
from pathlib import Path
import xml.etree.ElementTree as ET


def read_junit_reports(root):
    root=Path(root).resolve()
    reports=[]
    for path in sorted((root/'target/surefire-reports').glob('TEST-*.xml'))[:100]:
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size>2_000_000:
            continue
        try:
            suite=ET.fromstring(path.read_bytes())
            reports.append({'suite':suite.get('name',''), 'tests':int(suite.get('tests','0')), 'failures':int(suite.get('failures','0')), 'errors':int(suite.get('errors','0')), 'skipped':int(suite.get('skipped','0')),
                            'cases':[{'name':case.get('name',''),'class':case.get('classname',''),'status':'failed' if case.find('failure') is not None or case.find('error') is not None else 'skipped' if case.find('skipped') is not None else 'passed'} for case in suite.findall('testcase')[:2000]]})
        except (ET.ParseError,ValueError,OSError):
            continue
    if not reports:return None
    return {'format':'junit_xml',**{key:sum(item[key] for item in reports) for key in ('tests','failures','errors','skipped')},'suites':reports}
