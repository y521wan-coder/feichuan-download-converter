using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class WorkerProtocolTests
{
    [TestMethod]
    public void CreateRequest_RejectsSensitiveFieldsAndSignedValues()
    {
        var fieldError = Assert.ThrowsExactly<WorkerProtocolException>(() =>
            WorkerProtocol.CreateRequest("one", "test", new
            {
                nested = new Dictionary<string, string> { ["secret_key"] = "must-not-cross" }
            }));
        Assert.AreEqual("sensitive_field", fieldError.Code);

        var valueError = Assert.ThrowsExactly<WorkerProtocolException>(() =>
            WorkerProtocol.CreateRequest("two", "test", new
            {
                text = "https://media.example/video?q-signature=must-not-cross"
            }));
        Assert.AreEqual("sensitive_value", valueError.Code);

        var xiaoeValueError = Assert.ThrowsExactly<WorkerProtocolException>(() =>
            WorkerProtocol.CreateRequest("three", "test", new
            {
                text = "https://media.xet.tech/replay.m3u8?sign=redacted-test-value"
            }));
        Assert.AreEqual("sensitive_value", xiaoeValueError.Code);
    }

    [TestMethod]
    public void SanitizeDiagnostic_RemovesSecretsAndSignedUrls()
    {
        var diagnostic = WorkerProtocol.SanitizeDiagnostic(
            "SecretKey=do-not-log https://cos.example/item?q-signature=do-not-log");

        Assert.IsFalse(diagnostic.Contains("do-not-log", StringComparison.Ordinal));
        StringAssert.Contains(diagnostic, "[已隐藏]");
        Assert.AreEqual(
            "媒体：[已隐藏签名地址]",
            WorkerProtocol.SanitizeDiagnostic(
                "媒体：https://media.xet.tech/replay.m3u8?sign=redacted-test-value"));
    }

    [TestMethod]
    public async Task Client_HandshakesAndClassifiesWithRealPythonWorker()
    {
        var root = FindRepositoryRoot();
        var workerRoot = Path.Combine(root, "worker");
        var workerMain = Path.Combine(workerRoot, "src", "worker_main.py");
        var environment = new Dictionary<string, string>
        {
            ["PYTHONPATH"] = Path.Combine(workerRoot, "src"),
            ["PYTHONDONTWRITEBYTECODE"] = "1",
            ["FEICHUAN_AUTO_UPDATE_CORE"] = "0",
            ["FEICHUAN_SOFTWARE_UPDATE_ENDPOINT"] = string.Empty,
            ["FEICHUAN_SETTINGS_PATH"] = Path.Combine(
                Path.GetTempPath(),
                $"feichuan-worker-test-{Guid.NewGuid():N}.json")
        };

        await using var client = new FeichuanWorkerClient(new WorkerLaunchOptions(
            "python",
            [workerMain],
            workerRoot,
            environment));
        var diagnostics = new List<string>();
        client.DiagnosticReceived += (_, line) => diagnostics.Add(line);

        var hello = await client.StartAsync().WaitAsync(TimeSpan.FromSeconds(15));
        Assert.AreEqual("hello.result", hello.Type);
        Assert.AreEqual("1.1", hello.Payload.GetProperty("worker_version").GetString());
        Assert.IsTrue(hello.Payload.GetProperty("capabilities")
            .EnumerateArray()
            .Any(value => value.GetString() == "douyin.note.images"));
        Assert.IsTrue(hello.Payload.GetProperty("capabilities")
            .EnumerateArray()
            .Any(value => value.GetString() == "xiaoe.capture.download"));

        var classified = await client.SendAsync(
            "link.classify",
            new { text = "复制打开 https://v.douyin.com/example/ 试试看" })
            .WaitAsync(TimeSpan.FromSeconds(15));
        Assert.AreEqual("link.classify.result", classified.Type);
        Assert.AreEqual("single_link", classified.Payload.GetProperty("source").GetString());
        Assert.AreEqual(
            "https://v.douyin.com/example/",
            classified.Payload.GetProperty("url").GetString());
        Assert.IsEmpty(diagnostics);
    }

    [TestMethod]
    public async Task Client_ReportsWorkerCrashDuringHandshake()
    {
        await using var client = new FeichuanWorkerClient(new WorkerLaunchOptions(
            "python",
            ["-c", "import sys; sys.stdin.readline(); sys.exit(23)"]));

        await Assert.ThrowsExactlyAsync<WorkerProcessException>(async () =>
            await client.StartAsync().WaitAsync(TimeSpan.FromSeconds(15)));
    }

    [TestMethod]
    public async Task Client_CancelTimeoutTerminatesUnresponsiveWorker()
    {
        const string script = """
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                kind = request['type']
                payload = {}
                if kind == 'hello':
                    payload = {'worker_version': '1.0'}
                elif kind == 'cancel':
                    payload = {'accepted': True, 'busy': True}
                elif kind == 'task.status':
                    payload = {'busy': True}
                else:
                    payload = {'accepted': True}
                response = {
                    'protocol': 'feichuan-worker', 'version': 1,
                    'id': request['id'], 'type': kind + '.result', 'payload': payload
                }
                print(json.dumps(response), flush=True)
            """;
        await using var client = new FeichuanWorkerClient(new WorkerLaunchOptions(
            "python",
            ["-c", script]));
        await client.StartAsync().WaitAsync(TimeSpan.FromSeconds(15));

        await client.CancelOrTerminateAsync(TimeSpan.FromMilliseconds(300));

        Assert.IsFalse(client.IsRunning, "取消超时后必须终止无响应的工作进程。 ");
    }

    private static string FindRepositoryRoot()
    {
        var current = new DirectoryInfo(Environment.CurrentDirectory);
        while (current is not null)
        {
            if (File.Exists(Path.Combine(current.FullName, "worker", "src", "worker_main.py")))
            {
                return current.FullName;
            }

            current = current.Parent;
        }

        throw new DirectoryNotFoundException("找不到合并仓库根目录。");
    }
}
