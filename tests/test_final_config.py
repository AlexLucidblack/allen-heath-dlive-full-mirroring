import copy
import unittest
from mirror_app import DEFAULT_CONFIG, FINAL_OPTIONS, SURFACE_MODELS, final_config


class FinalConfigTests(unittest.TestCase):
    def test_old_settings_migrate_with_addresses_preserved(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['A']['ip'] = '192.168.1.75'
        out = final_config(cfg, migrate=True)
        self.assertEqual(out['direction'], 'A_to_B')
        self.assertEqual(out['A']['ip'], '192.168.1.75')
        for key, value in FINAL_OPTIONS.items():
            self.assertEqual(out[key], value)
        self.assertEqual(cfg['direction'], 'both')

    def test_public_settings_force_options_and_reject_both(self):
        for direction in ('A_to_B', 'B_to_A'):
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg['direction'] = direction
            cfg['probe'] = 'none'
            cfg['scene_sync'] = True
            self.assertEqual(final_config(cfg)['direction'], direction)
            self.assertEqual(final_config(cfg)['probe'], 'capture_all')
            self.assertFalse(final_config(cfg)['scene_sync'])
        with self.assertRaises(ValueError):
            final_config(DEFAULT_CONFIG)

    def test_surface_models_and_legacy_label(self):
        for model in SURFACE_MODELS:
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            cfg['direction'] = 'B_to_A'
            cfg['A']['surface_model'] = model
            self.assertEqual(final_config(cfg)['A']['surface_model'], model)
        cfg['A']['surface_model'] = 'Old free text'
        with self.assertRaises(ValueError):
            final_config(cfg)
        self.assertEqual(final_config(cfg, migrate=True)['A']['surface_model'], 'S5000')
