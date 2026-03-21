import socket
from redis import Redis


pipe: Redis.pipeline = Redis().pipeline(transaction=False)
print(pipe)