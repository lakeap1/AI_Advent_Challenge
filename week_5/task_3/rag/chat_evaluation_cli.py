"""Local evidence export and independently prepared assessment import."""

import argparse
import json
from pathlib import Path

from .chat_evaluation import ChatEvaluationService


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--profile-id',type=int,required=True)
    commands=parser.add_subparsers(dest='action',required=True)
    export=commands.add_parser('export')
    export.add_argument('run_id')
    export.add_argument('output',type=Path)
    imported=commands.add_parser('import')
    imported.add_argument('report',type=Path)
    args=parser.parse_args(argv)
    service=ChatEvaluationService(args.data_dir)
    if args.action=='export':
        report=service.export(args.run_id,profile_id=args.profile_id)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(report['evidence_sha256'])
    else:
        report=json.loads(args.report.read_text(encoding='utf-8'))
        saved=service.apply_assessment(report,profile_id=args.profile_id)
        print(saved['id'],saved['assessment_status'])


if __name__=='__main__':
    main()
