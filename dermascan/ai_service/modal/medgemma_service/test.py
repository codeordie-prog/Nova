import socket
from pathlib import Path
import os
import time
import json
import redis

from dermascan.ai_service.modal.medgemma_service.gateways.endpoints import _redis

print(_redis.xlen("medgemma:jobs"))