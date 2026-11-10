"""Behavioral checks on real CPU-encoded clips, without network or models."""
import sys
import statistics
import tempfile
import unittest
import wave
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/analysis'))


def scene(index, different=False):
    image = Image.new('RGB', (320, 180), '#344f6a')
    draw = ImageDraw.Draw(image)
    if different:
        for x in range(0, 320, 24):
            draw.rectangle((x, 0, x + 11, 179), fill='#f4cc33')
        draw.line((0, 160 - index * 3, 319, 20 + index * 3), fill='red', width=15)
    else:
        # Large irregular structures survive resizing and lossy encoding.
        for j in range(8):
            x = (31 * j + index * 19) % 270
            y = (23 * j + index * 11) % 130
            color = ((j * 37 + 90) % 256, (index * 23 + j * 19) % 256, (j * 67) % 256)
            draw.rectangle((x, y, x + 30 + j, y + 24), fill=color)
        draw.ellipse((index * 9 % 240, 20, index * 9 % 240 + 65, 100), fill='white')
        draw.line((0, 179, 100 + index * 9, 0), fill='black', width=8)
    return image


def video(path, images, size=(320, 180), rate=4, crf=18):
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=rate)
        stream.width, stream.height = size
        stream.pix_fmt = 'yuv420p'
        stream.options = {'crf': str(crf), 'preset': 'ultrafast'}
        for image in images:
            frame = av.VideoFrame.from_image(image.resize(size))
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


class MediaFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_trimmed_resized_reencoded_content_matches_with_frame_evidence(self):
        # Catches treating exact bytes as the only match or penalizing an outro.
        import media_fingerprints as media
        a, b = self.folder / 'a.mp4', self.folder / 'b.mp4'
        images = [scene(i) for i in range(20)]
        video(a, images)
        video(b, images[4:16] + [scene(i, different=True) for i in range(8)],
              size=(160, 90), crf=29)
        fa = media.fingerprint(a, interval_seconds=0.5)
        fb = media.fingerprint(b, interval_seconds=0.5)
        result = media.compare_fingerprints(fa, fb)
        self.assertFalse(result['exact_file_match'])
        self.assertTrue(result['perceptual_candidate'], result['directions'])
        self.assertGreaterEqual(result['directions']['a_to_b']['matching_coverage'], 0.6)
        matched = [p for p in result['directions']['a_to_b']['nearest_frames'] if p['match']]
        self.assertGreaterEqual(len(matched), 3)
        self.assertIn('phash_distance', matched[0])
        self.assertIn('time_a_seconds', matched[0])
        self.assertIn('time_b_seconds', matched[0])
        reverse_matches = [p for p in result['directions']['b_to_a']['nearest_frames'] if p['match']]
        self.assertAlmostEqual(statistics.median(p['time_a_seconds'] - p['time_b_seconds']
                                                for p in reverse_matches), 1.0, delta=0.5)
        self.assertEqual(len(fa['source']['sha256']), 64)

    def test_horizontally_mirrored_sequence_matches_as_candidate_with_frame_evidence(self):
        import media_fingerprints as media
        original, mirrored = self.folder / 'original.mp4', self.folder / 'mirrored.mp4'
        images = [scene(i) for i in range(16)]
        video(original, images)
        video(mirrored, [ImageOps.mirror(image) for image in images])

        result = media.compare_fingerprints(
            media.fingerprint(original, interval_seconds=0.5),
            media.fingerprint(mirrored, interval_seconds=0.5),
        )

        self.assertFalse(result['exact_file_match'])
        self.assertTrue(result['perceptual_candidate'], result['directions'])
        self.assertGreaterEqual(result['directions']['a_to_b']['temporally_consistent_frames'], 3)
        self.assertIn('candidate_shared_visual_sequence', result['decision'])

    def test_embedded_segment_in_multiscene_montage_is_a_partial_candidate_only(self):
        import media_fingerprints as media
        original, montage = self.folder / 'original_segment.mp4', self.folder / 'montage.mp4'
        shared = [scene(i) for i in range(16)]
        video(original, shared)
        video(montage, [scene(i, different=True) for i in range(8)] + shared
              + [scene(i + 20, different=True) for i in range(8)])

        result = media.compare_fingerprints(
            media.fingerprint(original, interval_seconds=0.5),
            media.fingerprint(montage, interval_seconds=0.5),
        )

        self.assertTrue(result['perceptual_candidate'], result['directions'])
        self.assertGreaterEqual(result['directions']['a_to_b']['distinct_target_frames'], 3)
        self.assertAlmostEqual(result['directions']['a_to_b']['median_offset_seconds_target_minus_source'], 2.0, delta=0.5)

    def test_title_card_prelude_does_not_hide_a_shared_clip(self):
        import media_fingerprints as media
        original, published = self.folder / 'original.mp4', self.folder / 'title_card_prelude.mp4'
        shared = [scene(i) for i in range(16)]
        title = Image.new('RGB', (320, 180), 'white')
        ImageDraw.Draw(title).text((30, 70), 'BREAKING NEWS', fill='black', stroke_width=2)
        video(original, shared)
        video(published, [title] * 8 + shared)

        result = media.compare_fingerprints(
            media.fingerprint(original, interval_seconds=0.5),
            media.fingerprint(published, interval_seconds=0.5),
        )

        self.assertTrue(result['perceptual_candidate'], result['directions'])
        self.assertAlmostEqual(
            result['directions']['a_to_b']['median_offset_seconds_target_minus_source'],
            2.0,
            delta=0.5,
        )

    def test_clearly_different_visual_sequences_do_not_match(self):
        import media_fingerprints as media
        a, b = self.folder / 'a.mp4', self.folder / 'different.mp4'
        video(a, [scene(i) for i in range(16)])
        video(b, [scene(i, different=True) for i in range(16)])
        result = media.compare_fingerprints(media.fingerprint(a, interval_seconds=0.5),
                                            media.fingerprint(b, interval_seconds=0.5))
        self.assertFalse(result['candidate_media_match'])
        self.assertFalse(result['perceptual_candidate'])

    def test_single_frame_clip_is_valid_but_has_insufficient_perceptual_evidence(self):
        import media_fingerprints as media
        path = self.folder / 'short.mp4'
        video(path, [scene(1)])
        fp = media.fingerprint(path)
        self.assertEqual(len(fp['frames']), 1)
        result = media.compare_fingerprints(fp, fp)
        self.assertTrue(result['exact_file_match'])
        self.assertFalse(result['perceptual_candidate'])
        self.assertEqual(result['directions']['a_to_b']['informative_frames'], 1)

    def test_uniform_frames_cannot_supply_perceptual_match_evidence(self):
        import media_fingerprints as media
        path = self.folder / 'blank.mp4'
        video(path, [Image.new('RGB', (320, 180), 'black')] * 16)
        fp = media.fingerprint(path, interval_seconds=0.5)
        result = media.compare_fingerprints(fp, fp)
        self.assertFalse(result['perceptual_candidate'])
        self.assertEqual(result['directions']['a_to_b']['informative_frames'], 0)
        self.assertTrue(result['exact_file_match'])

    def test_audio_only_and_invalid_files_fail_explicitly(self):
        import media_fingerprints as media
        audio = self.folder / 'audio.wav'
        with wave.open(str(audio), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b'\0\0' * 800)
        with self.assertRaisesRegex(ValueError, 'video stream'):
            media.fingerprint(audio)
        invalid = self.folder / 'invalid.mp4'
        invalid.write_bytes(b'not a media file')
        with self.assertRaisesRegex(ValueError, 'decode|open'):
            media.fingerprint(invalid)


if __name__ == '__main__':
    unittest.main()
