import os
import socket
import subprocess
import time
import json
import dateutil.parser
import urllib.request
from pathlib import Path

import certifi
from packaging.requirements import Requirement
from packaging.utils import (
    canonicalize_name,
)


HOME = Path(os.getcwd())
OUT = Path(os.getenv("out"))
PYTHON_WITH_MITM_PROXY = os.getenv("pythonWithMitmproxy")
FILTER_PYPI_RESPONSE_SCRIPTS = os.getenv("filterPypiResponsesScript")
PIP_FLAGS = os.getenv("pipFlags")
REQUIREMENTS_LIST = os.getenv("requirementsList")
REQUIREMENTS_FILES = os.getenv("requirementsFiles")


def get_max_date():
    try:
        return int(os.getenv("pypiSnapshotDate"))
    except ValueError:
        return dateutil.parser.parse(os.getenv("pypiSnapshotDate"))


def get_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_mitmproxy(port):
    proc = subprocess.Popen(
        [
            f"{PYTHON_WITH_MITM_PROXY}/bin/mitmdump",
            "--listen-port",
            str(port),
            "--anticache",
            "--ignore-hosts",
            ".*files.pythonhosted.org.*",
            "--script",
            FILTER_PYPI_RESPONSE_SCRIPTS,
        ],
        env={"pypiSnapshotDate": os.getenv("pypiSnapshotDate"), "HOME": HOME},
    )
    return proc


def wait_for_proxy(proxy_port):
    timeout = time.time() + 60 * 5
    req = urllib.request.Request("http://pypi.org")
    req.set_proxy(f"127.0.0.1:{proxy_port}", "http")

    while time.time() < timeout:
        try:
            res = urllib.request.urlopen(req, None, 5)
            if res.status < 400:
                break
        except urllib.error.URLError:
            pass
        finally:
            time.sleep(1)


# as we only proxy *some* calls, we need to combine upstream
# ca certificates and the one from mitm proxy
def generate_ca_bundle(path):
    with open(HOME / ".mitmproxy/mitmproxy-ca-cert.pem", "r") as f:
        mitmproxy_cacert = f.read()
    with open(certifi.where(), "r") as f:
        certifi_cacert = f.read()
    with open(path, "w") as f:
        f.write(mitmproxy_cacert)
        f.write("\n")
        f.write(certifi_cacert)
    return path


def pip(*args):
    subprocess.run(["pip", *args], check=True)


if __name__ == "__main__":
    print(
        f"selected maximum release date for python packages: {get_max_date()}"
    )  # noqa: E501
    proxy_port = get_free_port()

    proxy = start_mitmproxy(proxy_port)
    wait_for_proxy(proxy_port)
    cafile = generate_ca_bundle(HOME / ".ca-cert.pem")

    flags = [
        PIP_FLAGS,
        "--proxy",
        f"https://localhost:{proxy_port}",
        "--progress-bar",
        "off",
        "--cert",
        cafile,
        "--report",
        str(OUT / "report.json"),
    ]
    for req in REQUIREMENTS_LIST.split(" "):
        if req:
            flags.append(req)
    for req in REQUIREMENTS_FILES.split(" "):
        if req:
            flags += ["-r", req]

    flags = " ".join(map(str, filter(None, flags))).split(" ")
    pip(
        "install",
        "--dry-run",
        "--ignore-installed",
        *flags,
    )
    proxy.kill()

    packages = dict()
    extras = ""
    with open(OUT / "report.json", "r") as f:
        report = json.load(f)

    for install in report["install"]:
        metadata = install["metadata"]
        name = canonicalize_name(metadata["name"])

        download_info = install["download_info"]
        url = download_info["url"]
        sha256 = (
            download_info.get("archive_info", {})
            .get("hashes", {})
            .get("sha256")  # noqa: E501
        )
        requirements = [
            Requirement(req) for req in metadata.get("requires_dist", [])
        ]  # noqa: E501
        dependencies = sorted(
            [
                canonicalize_name(req.name)
                for req in requirements
                if not req.marker or req.marker.evaluate({"extra": extras})
            ]
        )
        packages[name] = dict(
            version=metadata["version"],
            dependencies=dependencies,
            url=url,
            sha256=sha256,
        )
    with open(OUT / "metadata.json", "w") as f:
        json.dump(packages, f, indent=2)
