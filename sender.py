import socket

sock = socket.socket()
sock.connect(("192.168.29.82", 9999))

sock.send(b"HELLO")

print(sock.recv(1024))

sock.close()