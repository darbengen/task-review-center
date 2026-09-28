#!/usr/bin/env python3
"""Local stdio JSONL <-> documented Codex Unix WebSocket transport. No HTTP server."""
import argparse, base64, hashlib, json, os, selectors, socket, struct, sys

def frame(payload, opcode=1):
    mask=os.urandom(4); n=len(payload)
    header=bytes([0x80|opcode,0x80|n]) if n<126 else bytes([0x80|opcode,0x80|126])+struct.pack('!H',n) if n<65536 else bytes([0x80|opcode,0x80|127])+struct.pack('!Q',n)
    return header+mask+bytes(b^mask[i%4] for i,b in enumerate(payload))

def decode(buffer):
    if len(buffer)<2:return None
    first, second=buffer[0],buffer[1]; n=second&127; pos=2
    if first&0x70:raise ValueError('Unsupported WebSocket extension')
    if n==126:
        if len(buffer)<4:return None
        n=struct.unpack('!H',buffer[2:4])[0];pos=4
    elif n==127:
        if len(buffer)<10:return None
        n=struct.unpack('!Q',buffer[2:10])[0];pos=10
    if n>64*1024*1024:raise ValueError('WebSocket response too large')
    masked=bool(second&128)
    if len(buffer)<pos+(4 if masked else 0)+n:return None
    mask=buffer[pos:pos+4] if masked else None;pos+=4 if masked else 0
    payload=buffer[pos:pos+n]
    if masked:payload=bytes(b^mask[i%4] for i,b in enumerate(payload))
    return first&15,bool(first&128),payload,buffer[pos+n:]

def run(path):
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);sock.settimeout(10);sock.connect(path)
    key=base64.b64encode(os.urandom(16)).decode()
    sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+key+'\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
    raw=b''
    while b'\r\n\r\n' not in raw:
        part=sock.recv(8192)
        if not part:raise ConnectionError('Core closed handshake')
        raw+=part
        if len(raw)>65536:raise ValueError('Invalid handshake')
    header, buffer=raw.split(b'\r\n\r\n',1)
    expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
    headers=dict(line.decode().split(':',1) for line in header.split(b'\r\n')[1:] if b':' in line)
    accept=next((v.strip() for k,v in headers.items() if k.lower()=='sec-websocket-accept'),None)
    if b' 101 ' not in header.split(b'\r\n')[0] or accept!=expected:raise ValueError('Core WebSocket handshake rejected')
    sock.settimeout(None)
    poll=selectors.DefaultSelector();poll.register(sock,selectors.EVENT_READ,'socket');poll.register(sys.stdin,selectors.EVENT_READ,'stdin')
    input_buffer=b''; fragments=b''
    try:
        while True:
            while True:
                decoded=decode(buffer)
                if decoded is None:break
                opcode,final,payload,buffer=decoded
                if opcode==8:return
                if opcode==9:sock.sendall(frame(payload,10));continue
                if opcode==10:continue
                if opcode==1:fragments=payload
                elif opcode==0:fragments+=payload
                else:raise ValueError('Unsupported frame type')
                if len(fragments)>64*1024*1024:raise ValueError('Fragmented response too large')
                if final:
                    json.loads(fragments)
                    sys.stdout.buffer.write(fragments+b'\n');sys.stdout.buffer.flush();fragments=b''
            for key,_ in poll.select():
                if key.data=='socket':
                    chunk=sock.recv(65536)
                    if not chunk:return
                    buffer+=chunk
                else:
                    chunk=os.read(sys.stdin.fileno(),65536)
                    if not chunk:return
                    input_buffer+=chunk
                    while b'\n' in input_buffer:
                        line,input_buffer=input_buffer.split(b'\n',1)
                        if line.strip():json.loads(line);sock.sendall(frame(line))
    finally:poll.close();sock.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--socket',required=True);args=parser.parse_args()
    try:run(args.socket)
    except Exception as error:
        # Distinguish dead transports from permission/protocol rejection. The
        # parent may recover the former but must not bypass the latter.
        unavailable=isinstance(error,(ConnectionError,TimeoutError,FileNotFoundError))
        print('Core connection failed: '+type(error).__name__,file=sys.stderr)
        sys.exit(74 if unavailable else 78)
