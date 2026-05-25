import socket

client = socket.socket()

client.connect(
    ("192.168.1.100", 9999)
)

client.send(
    b"HELLO WORLD"
)

print(
    client.recv(1024)
)

client.close()