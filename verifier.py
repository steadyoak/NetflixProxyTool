import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def verify_sni_server(ip, port=443, sni_host="www.netflix.com", timeout=3.0):
    """
    验证一个 IP 是否可作为 Netflix 的 SNI 反代服务器。
    连接到 IP:port，使用 sni_host 进行 TLS 握手。
    成功则返回 (True, latency_ms, info)，失败返回 (False, 9999, error_str)。
    """
    start_time = time.time()
    sock = None
    ss = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        ss = ctx.wrap_socket(sock, server_hostname=sni_host)
        # 获取证书
        cert_bin = ss.getpeercert(binary_form=True)
        latency = int((time.time() - start_time) * 1000)
        
        # 尝试发送 HTTP HEAD 请求确认连通性
        req = f"HEAD / HTTP/1.1\r\nHost: {sni_host}\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n".encode()
        ss.sendall(req)
        
        # 接收部分响应
        try:
            resp = ss.recv(512)
            if resp:
                return True, latency, f"成功 (延迟: {latency}ms, 响应: {resp[:20].decode(errors='ignore').strip()})"
        except Exception:
            pass
            
        # 即使 recv 超时，如果 TLS 握手完成且拿到了对端证书，也是有效的 SNI 代理
        if cert_bin:
            return True, latency, f"成功 (TLS握手成功, 延迟: {latency}ms)"
        else:
            return False, 9999, "未能获取对端证书"
            
    except socket.timeout:
        return False, 9999, "连接超时"
    except ConnectionRefusedError:
        return False, 9999, "端口连接被拒绝"
    except Exception as e:
        return False, 9999, f"握手/连接失败: {str(e)}"
    finally:
        if ss:
            try:
                ss.close()
            except:
                pass
        elif sock:
            try:
                sock.close()
            except:
                pass

def batch_verify_sni(ips, port=443, sni_host="www.netflix.com", timeout=3.0, max_workers=10):
    """
    多线程并发验证多个 IP，返回按延迟从低到高排序的可用 IP 列表: [(ip, latency, info), ...]
    """
    valid_results = []
    failed_results = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ip = {
            executor.submit(verify_sni_server, ip, port, sni_host, timeout): ip
            for ip in set(ips)
        }
        for future in as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                ok, latency, msg = future.result()
                if ok:
                    valid_results.append((ip, latency, msg))
                else:
                    failed_results.append((ip, latency, msg))
            except Exception as e:
                failed_results.append((ip, 9999, str(e)))
                
    valid_results.sort(key=lambda x: x[1])
    return valid_results, failed_results

if __name__ == "__main__":
    import sys
    test_ip = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    print(f"测试 SNI 反代可用性: {test_ip}")
    ok, lat, msg = verify_sni_server(test_ip, timeout=2.0)
    print(f"结果: ok={ok}, lat={lat:.1f}ms, msg={msg}")
