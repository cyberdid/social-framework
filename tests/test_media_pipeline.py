"""Contracts for read-only, resumable local media indexing."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class MediaPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def executable(self, name, body):
        path = self.root / name
        path.write_text('#!/usr/bin/env python3\n' + body, encoding='utf-8')
        path.chmod(0o755)
        return path

    def test_sha256_file_returns_streamed_content_digest(self):
        import media_pipeline as media
        path = self.root / 'sample.mp4'
        path.write_bytes(b'local media bytes')

        self.assertEqual(media.sha256_file(path), hashlib.sha256(b'local media bytes').hexdigest())

    def test_probe_media_parses_ffprobe_json_and_reports_missing_tool(self):
        import media_pipeline as media
        path = self.root / 'sample.mp4'
        path.write_bytes(b'not decoded by this fake')
        fake = self.executable('ffprobe', "import json\nprint(json.dumps({'streams': [{'codec_type': 'video'}]}))\n")

        result = media.probe_media(path, ffprobe=str(fake))
        missing = media.probe_media(path, ffprobe=str(self.root / 'not-installed'))

        self.assertEqual(result, {'status': 'ok', 'data': {'streams': [{'codec_type': 'video'}]}})
        self.assertEqual(missing['status'], 'unavailable')

    def test_probe_media_reports_invalid_json(self):
        import media_pipeline as media
        path = self.root / 'sample.mp4'
        path.write_bytes(b'fixture')
        fake = self.executable('invalid-ffprobe', "print('not json')\n")

        result = media.probe_media(path, ffprobe=str(fake))

        self.assertEqual(result['status'], 'invalid_json')
        self.assertIn('invalid JSON', result['error'])

    def test_probe_media_resolves_paths_before_passing_them_to_ffprobe(self):
        import media_pipeline as media
        fake = self.executable('echo-ffprobe-arg', "import json, sys\nprint(json.dumps({'arg': sys.argv[-1]}))\n")

        result = media.probe_media('-clip.mp4', ffprobe=str(fake))

        self.assertEqual(result['data']['arg'], str(Path('-clip.mp4').resolve()))

    def test_manifest_keeps_provenance_separate_from_verified_facts_and_is_resumable(self):
        import media_pipeline as media
        path = self.root / 'clip.mp4'
        content = b'fixture video payload'
        path.write_bytes(content)
        count = self.root / 'probe-calls'
        probe_payload = {'format': {'format_name': 'mov,mp4'},
                         'streams': [{'codec_type': 'video', 'codec_name': 'h264',
                                      'width': 1920, 'height': 1080}]}
        fake_probe = self.executable('ffprobe',
            f"from pathlib import Path\nimport json\np = Path({str(count)!r})\np.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\nprint(json.dumps({probe_payload!r}))\n")
        fake_ffmpeg = self.executable('ffmpeg', "raise SystemExit(0)\n")
        source = {
            'source_locator': 'https://t.me/example/12',
            'local_path': str(path),
            'fetched_at': '2026-11-07T12:00:00Z',
            'provenance': {'collector': 'local-test', 'snapshot': 'fixture-1'},
            'model_observations': {'possible_scene': 'street'},
        }
        input_manifest, output_manifest = self.root / 'input.jsonl', self.root / 'indexed.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        first = media.index_manifest(input_manifest, output_manifest, ffprobe=str(fake_probe),
                                     ffmpeg=str(fake_ffmpeg), strict_decode=True)
        first_bytes = output_manifest.read_bytes()
        second = media.index_manifest(input_manifest, output_manifest, ffprobe=str(fake_probe),
                                      ffmpeg=str(fake_ffmpeg), strict_decode=True)
        rows = [json.loads(line) for line in output_manifest.read_text(encoding='utf-8').splitlines()]
        row = rows[0]

        self.assertEqual(first['status_counts'], {'ok': 1})
        self.assertEqual(second['status_counts'], {'ok': 1})
        self.assertEqual(first_bytes, output_manifest.read_bytes())
        self.assertEqual(count.read_text(), '1')
        self.assertEqual(row['source'], source)
        self.assertEqual(row['media_index']['verified_media_facts']['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(row['media_index']['verified_media_facts']['size_bytes'], len(content))
        self.assertEqual(row['media_index']['verified_media_facts']['media_type'], 'video/mp4')
        self.assertEqual(row['media_index']['verified_media_facts']['ffprobe'], probe_payload)
        self.assertEqual(row['media_index']['decode_check']['status'], 'passed')
        self.assertNotIn('model_observations', row['media_index']['verified_media_facts'])

    def test_manifest_hashes_each_new_file_once(self):
        import media_pipeline as media
        path = self.root / 'clip.mp4'
        path.write_bytes(b'fixture')
        fake_probe = self.executable('ffprobe', "import json\nprint(json.dumps({'streams': []}))\n")
        source = {'source_locator': 'fixture:clip', 'local_path': str(path),
                  'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        with patch.object(media, 'sha256_file', wraps=media.sha256_file) as hash_file:
            media.index_manifest(input_manifest, output_manifest, ffprobe=str(fake_probe))

        self.assertEqual(hash_file.call_count, 1)

    def test_manifest_records_probe_and_decode_failures_as_statuses(self):
        import media_pipeline as media
        path = self.root / 'broken.mkv'
        path.write_bytes(b'broken media')
        bad_probe = self.executable('bad-ffprobe', "import sys\nsys.stderr.write('invalid container')\nsys.exit(2)\n")
        bad_ffmpeg = self.executable('bad-ffmpeg', "import sys\nsys.stderr.write('decode error')\nsys.exit(1)\n")
        source = {'source_locator': 'fixture:broken', 'local_path': str(path),
                  'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        summary = media.index_manifest(input_manifest, output_manifest, ffprobe=str(bad_probe),
                                       ffmpeg=str(bad_ffmpeg), strict_decode=True)
        row = json.loads(output_manifest.read_text(encoding='utf-8'))

        self.assertEqual(summary['status_counts'], {'decode_failed': 1})
        self.assertEqual(row['media_index']['status'], 'decode_failed')
        self.assertEqual(row['media_index']['probe']['status'], 'failed')
        self.assertEqual(row['media_index']['decode_check']['status'], 'failed')
        self.assertIn('invalid container', row['media_index']['probe']['error'])

    def test_missing_local_file_has_explicit_status_without_network_fallback(self):
        import media_pipeline as media
        source = {'source_locator': 'https://example.invalid/video.mp4',
                  'local_path': 'missing.mp4', 'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        summary = media.index_manifest(input_manifest, output_manifest,
                                       ffprobe=str(self.root / 'missing-ffprobe'))
        row = json.loads(output_manifest.read_text(encoding='utf-8'))

        self.assertEqual(summary['status_counts'], {'file_missing': 1})
        self.assertEqual(row['media_index']['status'], 'file_missing')
        self.assertIsNone(row['media_index']['verified_media_facts']['sha256'])

    def test_manifest_rejects_ffmpeg_protocol_paths_as_input_errors(self):
        import media_pipeline as media
        source = {'source_locator': 'fixture:protocol-wrapper', 'local_path': 'concat:/tmp/a.mp4',
                  'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        summary = media.index_manifest(input_manifest, output_manifest)
        row = json.loads(output_manifest.read_text(encoding='utf-8'))

        self.assertEqual(summary['status_counts'], {'input_error': 1})
        self.assertEqual(row['media_index']['status'], 'input_error')

    def test_strict_decode_marks_missing_ffmpeg_as_unavailable(self):
        import media_pipeline as media
        path = self.root / 'clip.mp4'
        path.write_bytes(b'fixture')
        fake_probe = self.executable('ffprobe', "import json\nprint(json.dumps({'streams': []}))\n")
        source = {'source_locator': 'fixture:clip', 'local_path': str(path),
                  'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        summary = media.index_manifest(input_manifest, output_manifest, ffprobe=str(fake_probe),
                                       ffmpeg=str(self.root / 'missing-ffmpeg'), strict_decode=True)
        row = json.loads(output_manifest.read_text(encoding='utf-8'))

        self.assertEqual(summary['status_counts'], {'decode_unavailable': 1})
        self.assertEqual(row['media_index']['decode_check']['status'], 'unavailable')

    def test_manifest_recovers_from_a_truncated_last_checkpoint(self):
        import media_pipeline as media
        path = self.root / 'clip.mp4'
        path.write_bytes(b'fixture')
        fake_probe = self.executable('ffprobe', "import json\nprint(json.dumps({'streams': []}))\n")
        source = {'source_locator': 'fixture:clip', 'local_path': str(path),
                  'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')
        output_manifest.write_text('{"record_id":"interrupted', encoding='utf-8')

        summary = media.index_manifest(input_manifest, output_manifest, ffprobe=str(fake_probe))

        self.assertEqual(summary['status_counts'], {'ok': 1})
        rows = [json.loads(line) for line in output_manifest.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 1)

    def test_output_manifest_rejects_same_symlink_and_hardlink_as_media(self):
        import media_pipeline as media
        media_path = self.root / 'clip.mp4'
        media_path.write_bytes(b'{"record_id":"existing"}\n')
        input_manifest = self.root / 'input.jsonl'
        source = {'source_locator': 'fixture:clip', 'local_path': str(media_path),
                  'fetched_at': None, 'provenance': {}}
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')
        hardlink = self.root / 'hardlink-output.jsonl'
        symlink = self.root / 'symlink-output.jsonl'
        os.link(media_path, hardlink)
        symlink.symlink_to(media_path)

        for label, output in [('same_path', media_path), ('hardlink', hardlink), ('symlink', symlink)]:
            with self.subTest(alias=label):
                before = media_path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'output manifest must not alias'):
                    media.index_manifest(input_manifest, output, ffprobe=str(self.root / 'missing-ffprobe'))
                self.assertEqual(media_path.read_bytes(), before)

    def test_local_playlist_probe_and_decode_only_allow_file_protocol(self):
        import media_pipeline as media
        playlist = self.root / 'playlist.m3u8'
        playlist.write_text('#EXTM3U\nhttps://example.invalid/segment.ts\n', encoding='utf-8')
        probe_args, ffmpeg_args = self.root / 'probe-args.json', self.root / 'ffmpeg-args.json'
        fake_probe = self.executable('capture-ffprobe',
            f"from pathlib import Path\nimport json, sys\nPath({str(probe_args)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "print(json.dumps({'streams': []}))\n")
        fake_ffmpeg = self.executable('capture-ffmpeg',
            f"from pathlib import Path\nimport json, sys\nPath({str(ffmpeg_args)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        source = {'source_locator': 'fixture:playlist', 'local_path': str(playlist),
                  'fetched_at': None, 'provenance': {}}
        input_manifest, output_manifest = self.root / 'in.jsonl', self.root / 'out.jsonl'
        input_manifest.write_text(json.dumps(source) + '\n', encoding='utf-8')

        media.index_manifest(input_manifest, output_manifest, ffprobe=str(fake_probe),
                             ffmpeg=str(fake_ffmpeg), strict_decode=True)
        probe_command = json.loads(probe_args.read_text(encoding='utf-8'))
        ffmpeg_command = json.loads(ffmpeg_args.read_text(encoding='utf-8'))

        for command in (probe_command, ffmpeg_command):
            with self.subTest(command=command[0]):
                whitelist_index = command.index('-protocol_whitelist')
                self.assertEqual(command[whitelist_index + 1], 'file')
                self.assertNotIn('https://example.invalid/segment.ts', command)

    def test_strict_decode_maps_all_video_and_audio_streams(self):
        import media_pipeline as media
        path = self.root / 'multi-track.mkv'
        path.write_bytes(b'fixture')
        args_file = self.root / 'ffmpeg-args.json'
        fake_ffmpeg = self.executable('capture-ffmpeg',
            f"from pathlib import Path\nimport json, sys\nPath({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]))\n")

        result = media.decode_media(path, ffmpeg=str(fake_ffmpeg))
        command = json.loads(args_file.read_text(encoding='utf-8'))
        mappings = [command[index + 1] for index, value in enumerate(command[:-1]) if value == '-map']

        self.assertEqual(result, {'status': 'passed'})
        self.assertEqual(mappings, ['0:v?', '0:a?'])


if __name__ == '__main__':
    unittest.main()
