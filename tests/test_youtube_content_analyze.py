import importlib.util
from pathlib import Path
import re
import unittest

class TopicCandidateTests(unittest.TestCase):
    def test_torture_marker_does_not_match_attempted_or_mobilized(self):
        path=Path(__file__).resolve().parents[1]/'src/analysis/youtube_content_analyze.py'
        spec=importlib.util.spec_from_file_location('analysis',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        pattern=module.TOPICS['torture_abuse']
        self.assertFalse(re.search(pattern,'он пытался спросить про мобилизованных'))
        self.assertTrue(re.search(pattern,'его пытали и били'))
        self.assertTrue(re.search(pattern,'сообщение о пытках и побитті'))

if __name__=='__main__':unittest.main()
