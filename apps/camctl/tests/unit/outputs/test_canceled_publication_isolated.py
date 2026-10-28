"""取消期间发布对账的纯状态分区；生产协作者在导入前隔离。"""

import importlib.util
from enum import IntEnum
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class Status(IntEnum):
    PENDING = 1
    PREPARING = 2
    PREPARED = 3
    PUBLISHING = 4
    PUBLISHED = 5
    FAILED = 6
    CANCELED = 7
    WITHDRAWN = 8


class Stub:
    def __init__(self, *args, **kwargs):
        pass


def load_handoff():
    names = ('camctl.contracts.enums', 'camctl.contracts.values',
        'camctl.contracts.workflow_errors', 'camctl.host_files.handoff',
        'camctl.host_files.io', 'camctl.host_files.models', 'camctl.host_files.tasks',
        'camctl.outputs.copy', 'camctl.persistence.models', 'camctl.session.supervision',
        'camctl.logging_runtime.diagnostics', 'camctl.logging_runtime.events',
        'camctl.operations.models')
    modules = {}
    for name in names:
        module = ModuleType(name)
        module.__getattr__ = lambda name: Stub
        modules[name] = module
    modules['camctl.contracts.enums'].enum_for = lambda name: Status
    modules['camctl.contracts.values'].ConsistencyError = type('ConsistencyError', (Exception,), {})
    modules['camctl.contracts.workflow_errors'].registered_error = lambda code: {'stage': 'publication'}
    modules['camctl.contracts.workflow_errors'].validate_error_details = lambda code, details: None
    name = 'camctl.outputs._isolated_cancel_handoff'
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[3] / 'src/camctl/outputs/handoff.py')
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class CanceledPublicationTests(unittest.TestCase):
    def setUp(self):
        self.h = load_handoff()
        self.digest = 'a' * 64
        self.facts = self.h.DeliveryFacts(19, Status.PUBLISHING, publication_intent_event_id=29,
            prepared_size=4, prepared_sha256=self.digest)
        self.absent = self.h.LocationObservation(True, False)
        self.present = self.h.LocationObservation(True, True, 4, self.digest)

    def classify(self, staging=None, ready=None, processing=None):
        locations = self.h.DeliveryLocations(staging or self.absent, ready or self.absent,
                                              processing or self.absent)
        return self.h.decide_canceled_publication(self.facts, locations)

    def test_staging_copy_is_canceled_without_publishing(self):
        self.assertEqual(self.classify(staging=self.present).value, 'cancel_staging')

    def test_ready_and_processing_copies_are_confirmed_for_withdrawal(self):
        for name in ('ready', 'processing'):
            with self.subTest(location=name):
                self.assertEqual(self.classify(**{name: self.present}).value, 'confirm_handoff')

    def test_absent_failed_observation_and_conflicting_copy_are_unknown(self):
        bad = self.h.LocationObservation(False, error=OSError('read failed'))
        conflict = self.h.LocationObservation(True, True, 4, 'b' * 64)
        for locations in ({}, {'ready': bad}, {'ready': conflict},
                          {'ready': self.present, 'processing': self.present},
                          {'staging': self.present, 'ready': self.present}):
            with self.subTest(locations=locations):
                self.assertEqual(self.classify(**locations).value, 'unknown')

    def test_terminal_delivery_is_not_reactivated(self):
        self.facts = self.h.DeliveryFacts(19, Status.WITHDRAWN)
        self.assertEqual(self.classify(staging=self.present).value, 'not_active')

    def test_only_a_saved_publication_intent_enters_reconciliation(self):
        for facts in (self.h.DeliveryFacts(19, Status.PREPARED, prepared_size=4,
                                             prepared_sha256=self.digest),
                      self.h.DeliveryFacts(19, Status.PUBLISHING, prepared_size=4,
                                             prepared_sha256=self.digest)):
            with self.subTest(status=facts.status, intent=facts.publication_intent_event_id):
                self.facts = facts
                with self.assertRaises(self.h.ConsistencyError):
                    self.classify(staging=self.present)
