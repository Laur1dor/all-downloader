"""Retention must keep container images, rollback aliases and recent snapshots."""
import importlib.util
from contextlib import redirect_stdout
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    'docker_maintenance', Path(__file__).resolve().parents[1] / 'scripts/docker-maintenance.py')
maintenance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maintenance)
NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def image(number, days, *tags):
    return {'Id': str(number), 'Created': (NOW-timedelta(days=days)).isoformat(),
            'RepoTags': list(tags)}


class DockerRetentionTests(unittest.TestCase):
    def test_keeps_two_newest_snapshots_per_project_and_removes_old_history(self):
        rows = [image(i, i+8, f'tiktok-bot-old-{i}-before:latest') for i in range(4)]
        rows += [image(i+10, i+8, f'anime-bot-before-fix{i}:snapshot') for i in range(4)]
        self.assertEqual({x['Id'] for x in maintenance.plan_images(rows, [], NOW)},
                         {'2', '3', '12', '13'})

    def test_all_container_references_are_pinned_including_stopped_containers(self):
        rows = [image(i, i+8, f'tiktok-bot-old-{i}-before:latest') for i in range(4)]
        containers = [{'Image':'2', 'State':{'Running':False}},
                      {'Image':'3', 'State':{'Running':True}}]
        self.assertEqual(maintenance.plan_images(rows, containers, NOW), [])

    def test_current_rollback_and_unknown_aliases_pin_entire_image(self):
        rows = [image(i, i+8, f'tiktok-bot-old-{i}-before:latest') for i in range(5)]
        rows[2]['RepoTags'].append('tiktok-bot-bot:previous')
        rows[3]['RepoTags'].append('tiktok-bot-bot:latest')
        rows[4]['RepoTags'].append('operator/manual-pin:archive')
        self.assertEqual(maintenance.plan_images(rows, [], NOW), [])

    def test_recent_history_and_unmanaged_images_are_never_candidates(self):
        rows = [image(i, i+1, f'tiktok-bot-old-{i}-before:latest') for i in range(5)]
        rows += [image(8,100,'postgres:17-alpine'),image(9,100,'anime-repair-probe:good'),
                 image(10,100)]
        self.assertEqual(maintenance.plan_images(rows, [], NOW), [])

    def test_cache_policy_bounds_unused_cache_without_volume_or_container_prune(self):
        args = maintenance.CACHE_ARGS
        self.assertEqual(args[:3], ['docker','builder','prune'])
        self.assertIn('--all',args)
        self.assertEqual(args[args.index('--max-used-space')+1],'1gb')
        self.assertEqual(args[args.index('--reserved-space')+1],'512mb')
        self.assertEqual(args[args.index('--min-free-space')+1],'5gb')

    def test_recent_rollback_tag_on_old_image_is_kept(self):
        rows = [image(i,100+i,f'anime-bot-before-old{i}:20260101') for i in range(4)]
        rows.append(image(9,200,'anime-bot-before-new-snapshot:20261028T073129Z'))
        self.assertNotIn('9',{x['Id']for x in maintenance.plan_images(rows,[],NOW)})
        self.assertEqual(maintenance.snapshot_at(rows[-1],NOW).date(),NOW.date())

    def test_short_snapshot_date_rolls_back_year_in_january(self):
        now=datetime(2027,1,3,tzinfo=timezone.utc)
        row=image(1,100,'tiktok-bot-fix-1231-before:latest')
        self.assertEqual(maintenance.snapshot_at(row,now),datetime(2026,12,31,tzinfo=timezone.utc))

    @unittest.skipIf(os.name == 'nt', 'Linux flock service')
    def test_apply_rechecks_new_container_references_before_removal(self):
        rows=[image(i,100+i,f'tiktok-bot-old-{i}-before:latest') for i in range(4)]
        # Both original candidates acquired container references after planning.
        fresh=[{'Id':str(i),'Image':str(i),'Name':f'/container{i}',
                'State':{'Running':True}} for i in (2,3)]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(maintenance,'LOCK',str(Path(folder)/'lock')), \
             patch.object(maintenance,'build_in_progress',return_value=False), \
             patch.object(maintenance,'inventory',side_effect=[
                 (rows,[]),(rows,fresh),(rows,fresh),(rows,fresh)]), \
             patch.object(maintenance,'command',return_value='inactive\n') as command, \
             patch('sys.argv',['maintenance','--apply']), redirect_stdout(io.StringIO()):
            maintenance.main()
        self.assertFalse(any(call.args[0][:3]==['docker','image','rm']
                             for call in command.call_args_list))
        self.assertTrue(any(call.args[0]==maintenance.CACHE_ARGS
                            for call in command.call_args_list))
