"""
Management command to batch-export QA training datasets to disk.

Usage:
    python manage.py export_qa_dataset --type agent_decisions --output /data/training/
    python manage.py export_qa_dataset --type all --since 2025-01-01 --output /data/training/
    python manage.py export_qa_dataset --type test_generation --limit 10000

Produces JSONL files ready for fine-tuning pipelines (LoRA, DPO, etc).
"""
import json
import os
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone as tz

from test_cases.models import (
    AgentMissionStep, AgentMission, TestRun, TestCase
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Export QA training datasets for model fine-tuning'

    def add_arguments(self, parser):
        parser.add_argument(
            '--type', '-t',
            type=str,
            required=True,
            choices=['agent_decisions', 'test_generation', 'api_interactions', 'all'],
            help='Dataset type to export'
        )
        parser.add_argument(
            '--output', '-o',
            type=str,
            default='./datasets/',
            help='Output directory (default: ./datasets/)'
        )
        parser.add_argument(
            '--since',
            type=str,
            default=None,
            help='Only include data after this ISO date (default: 90 days ago)'
        )
        parser.add_argument(
            '--limit', '-l',
            type=int,
            default=10000,
            help='Max records per dataset type (default: 10000)'
        )

    def handle(self, *args, **options):
        dataset_type = options['type']
        output_dir = options['output']
        limit = options['limit']
        since_str = options['since']

        if since_str:
            since = tz.datetime.fromisoformat(since_str.replace('Z', '+00:00'))
        else:
            since = tz.now() - timedelta(days=90)

        os.makedirs(output_dir, exist_ok=True)

        types_to_export = ['agent_decisions', 'test_generation', 'api_interactions'] \
            if dataset_type == 'all' else [dataset_type]

        for dt in types_to_export:
            self.stdout.write(f'Exporting {dt} since {since.isoformat()}...')

            if dt == 'agent_decisions':
                records = self._build_agent_decisions(since, limit)
            elif dt == 'test_generation':
                records = self._build_test_generation(since, limit)
            elif dt == 'api_interactions':
                records = self._build_api_interactions(since, limit)
            else:
                continue

            output_path = os.path.join(output_dir, f'qa_{dt}.jsonl')
            with open(output_path, 'w') as f:
                for record in records:
                    f.write(json.dumps(record, default=str) + '\n')

            self.stdout.write(
                self.style.SUCCESS(f'  ✓ {len(records)} records → {output_path}')
            )

        self.stdout.write(self.style.SUCCESS('Done.'))

    def _build_agent_decisions(self, since, limit):
        steps = AgentMissionStep.objects.filter(
            created_at__gte=since,
            mission__status__in=['completed', 'error']
        ).select_related('mission', 'mission__collection').order_by(
            'mission_id', 'step_number'
        )[:limit * 3]

        records = []
        for step in steps:
            if step.action_type == 'FINISH' or not step.thought:
                continue

            mission = step.mission
            records.append({
                'instruction': f"You are a QA agent testing '{mission.collection.name}'. User story: {mission.user_story}",
                'context': {
                    'scenarios': mission.scenarios,
                    'categories': mission.categories,
                    'mission_type': mission.mission_type,
                    'step_number': step.step_number,
                    'previous_actions': list(
                        mission.steps.filter(step_number__lt=step.step_number)
                        .values_list('action_type', flat=True)
                    ),
                },
                'action_chosen': {
                    'type': step.action_type,
                    'details': step.details,
                    'thought': step.thought,
                },
                'outcome': {
                    'status': step.status,
                    'response_status': step.response_status,
                    'response_preview': (step.response_body or '')[:500],
                },
                'quality_score': 1.0 if step.status == 'passed' else 0.0,
            })

            if len(records) >= limit:
                break

        return records

    def _build_test_generation(self, since, limit):
        runs = TestRun.objects.filter(
            executed_at__gte=since,
            test_case__ai_generated=True,
            triggered_by__in=['ai', 'ai_agent', 'manual']
        ).select_related(
            'test_case', 'test_case__endpoint'
        ).order_by('-executed_at')[:limit]

        records = []
        for run in runs:
            tc = run.test_case
            ep = tc.endpoint
            if not ep:
                continue

            records.append({
                'endpoint_schema': {
                    'method': ep.method,
                    'url': ep.url,
                    'name': ep.name,
                    'description': ep.description,
                    'request_body': ep.request_body,
                    'auth_type': ep.auth_type,
                },
                'scenario': {
                    'category': tc.category,
                    'priority': tc.priority,
                    'user_story': tc.user_story,
                    'tags': tc.tags,
                },
                'generated_test': {
                    'name': tc.name,
                    'description': tc.description,
                    'headers': tc.headers,
                    'body': tc.body,
                    'query_params': tc.query_params,
                    'expected_status': tc.expected_status,
                    'assertions': tc.assertions,
                    'test_script': tc.test_script,
                },
                'outcome': {
                    'passed': run.status == 'passed',
                    'actual_status': run.response_status,
                    'error': run.error_message,
                },
                'quality_score': 1.0 if run.status == 'passed' else 0.0,
            })

        return records

    def _build_api_interactions(self, since, limit):
        runs = TestRun.objects.filter(
            executed_at__gte=since,
            response_status__isnull=False
        ).select_related(
            'test_case', 'test_case__endpoint'
        ).order_by('-executed_at')[:limit]

        records = []
        for run in runs:
            tc = run.test_case
            ep = tc.endpoint if tc else None

            records.append({
                'method': ep.method if ep else 'UNKNOWN',
                'url_pattern': ep.url if ep else '/',
                'request': {
                    'headers': self._anonymize_headers(tc.headers),
                    'body': self._anonymize_payload(tc.body) if tc.body else {},
                    'query_params': tc.query_params,
                },
                'response': {
                    'status': run.response_status,
                    'body_preview': self._truncate_response(run.response_body),
                    'time_ms': run.response_time_ms,
                },
                'context': {
                    'category': tc.category,
                    'runner_type': tc.runner_type,
                    'was_ai_generated': tc.ai_generated,
                },
                'quality_score': 1.0 if run.status == 'passed' else 0.0,
            })

        return records

    def _anonymize_payload(self, data):
        if not isinstance(data, dict):
            return data
        anonymized = {}
        for key, value in data.items():
            if isinstance(value, str):
                lower_key = key.lower()
                if 'email' in lower_key or '@' in str(value):
                    anonymized[key] = '<EMAIL>'
                elif any(s in lower_key for s in ['password', 'token', 'secret', 'key']):
                    anonymized[key] = '<REDACTED>'
                elif 'name' in lower_key:
                    anonymized[key] = '<NAME>'
                elif value.isdigit():
                    anonymized[key] = '<INTEGER>'
                elif value.startswith('http'):
                    anonymized[key] = '<URL>'
                else:
                    anonymized[key] = f'<STRING:{len(value)}>'
            elif isinstance(value, (int, float)):
                anonymized[key] = f'<{type(value).__name__.upper()}>'
            elif isinstance(value, bool):
                anonymized[key] = value
            elif isinstance(value, dict):
                anonymized[key] = self._anonymize_payload(value)
            elif isinstance(value, list):
                anonymized[key] = [
                    self._anonymize_payload(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                anonymized[key] = value
        return anonymized

    def _anonymize_headers(self, headers):
        if not isinstance(headers, dict):
            return headers
        safe = {}
        for key, value in headers.items():
            lower = key.lower()
            if any(s in lower for s in ['auth', 'token', 'key', 'cookie', 'session']):
                safe[key] = '<REDACTED>'
            else:
                safe[key] = value
        return safe

    def _truncate_response(self, body, max_len=500):
        if not body:
            return ''
        text = str(body)
        return text[:max_len] + '...' if len(text) > max_len else text
