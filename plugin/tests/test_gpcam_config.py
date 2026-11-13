"""The skill's gpCAM configuration, run through tsuchinoko's real engine offline.

GPCAMInProcessEngine is built with ``tsuchinoko run``'s defaults and configured through
NATSService's configure handler (the tsuchinoko_configure path); then Core's iteration
steps run in-process against the sim focus model. Each measurement is told with
variance 1.0, as tsuchinoko's TiledReader does, and scored like qerlin_optimize:
SCORE_SCALE / FWHM, FWHM from the scorer calibration (FWHM^2 = 5.56 sigma^2 + 13.5).
"""
import asyncio
import json
import re
import warnings
from importlib.resources import files
from types import SimpleNamespace

import numpy as np
import pytest

from lightfall_endstation_602.optimize import SCORE_SCALE
from lightfall_endstation_602.sim.model import FocusModel

SKILL = files("lightfall_endstation_602.skills").joinpath("qerlin_optimization")


def _payload():
    return json.loads(SKILL.joinpath("references", "gpcam-configure.json").read_text())


def test_skill_inlines_the_reference_payload():
    text = SKILL.joinpath("SKILL.md").read_text(encoding="utf-8")
    block = re.search(r"```json\n(.*?)```", text, re.S).group(1)
    assert json.loads(block) == _payload()
    p = _payload()
    assert p["acquisition_function"] != "variance"
    assert len(p["hyperparameters"]) == len(p["hyperparameter_bounds"]) == 1 + len(
        p["parameter_bounds"])


class _Msg:
    def __init__(self, payload):
        self.data, self.reply, self.out = json.dumps(payload).encode(), "inbox", None

    async def respond(self, raw):
        self.out = json.loads(raw)


def _fwhm(model, pitch, x):
    return float(np.sqrt(5.56 * model.sigma_px(pitch, x) ** 2 + 13.5))


def test_configuration_finds_the_hidden_optimum():
    pytest.importorskip("tsuchinoko.nats.service")
    from tsuchinoko.adaptive import Data
    from tsuchinoko.adaptive.gpCAM_in_process import GPCAMInProcessEngine
    from tsuchinoko.nats.service import NATSService

    warnings.filterwarnings("ignore")
    np.random.seed(0)
    model = FocusModel()  # seed None: best pitch 104.0, best Sample X -0.80 mm
    engine = GPCAMInProcessEngine(dimensionality=2,
                                  parameter_bounds=[(0.0, 100.0), (0.0, 100.0)],
                                  hyperparameters=[100.0, 10.0, 10.0],
                                  hyperparameter_bounds=[(0.1, 1e5)] * 3)
    msg = _Msg(_payload())
    asyncio.run(NATSService(SimpleNamespace(adaptive_engine=engine), None)
                ._handle_configure(msg))
    assert msg.out == {"status": "ok"}
    engine.reset()  # Core's Starting transition
    data, pos = Data(dimensionality=2), (0.0, 0.0)
    for _ in range(30):
        target = tuple(float(v) for v in engine.request_targets(pos)[0])
        data.inject_new([(target, SCORE_SCALE / _fwhm(model, *target), 1.0, {})])
        engine.update_measurements(data)
        engine.train()
        pos = target
    best = np.asarray(data.positions)[int(np.argmax(data.scores))]
    assert abs(best[0] - 104.0) < 0.5 and abs(best[1] + 0.80) < 0.01
    assert _fwhm(model, *best) < 5.2  # the floor is 5.1 px
