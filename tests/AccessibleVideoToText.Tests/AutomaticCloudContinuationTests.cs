using System.Reflection;
using AccessibleVideoToText.App;
using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class AutomaticCloudContinuationTests
{
    [TestMethod]
    [DataRow(0)]
    [DataRow(11)]
    public void CloudPreflight_ContinuesWithoutDialogsAndRetainsOutputValidation(int usedHours)
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try { VerifyPreflight(usedHours); }
            catch (Exception exception) { failure = exception; }
        }) { IsBackground = true };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        Assert.IsTrue(thread.Join(TimeSpan.FromSeconds(15)), "云前置流程不应停在确认对话框。");
        if (failure is not null) throw failure;
    }

    private static void VerifyPreflight(int usedHours)
    {
        var directory = Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests", Guid.NewGuid().ToString("N"));
        var paths = new LocalDataPaths(directory);
        var store = new DpapiSettingsStore(paths);
        var credentials = new CloudCredentials("123456789", "TEST-ONLY-ID", "TEST-ONLY-KEY");
        store.SaveCredentialsAsync(credentials, CancellationToken.None).GetAwaiter().GetResult();
        var ledger = new JsonUsageLedger(paths);
        if (usedHours > 0)
            ledger.AddSuccessfulDurationAsync(credentials.SecretId, TimeSpan.FromHours(usedHours), CancellationToken.None)
                .GetAwaiter().GetResult();
        try
        {
            using var form = new MainForm(); // Never shown: no production settings/credentials are loaded.
            SetField(form, "localDataPaths", paths);
            SetField(form, "settingsStore", store);
            SetField(form, "usageLedger", ledger);
            SetField(form, "mediaProbe", new FakeProbe());
            var notifications = 0;
            SetField(form, "completionNotifier", new TaskCompletionNotifier(() => notifications++));
            var statuses = new List<string>();
            var label = (Label)GetField(form, "statusLabel");
            label.TextChanged += (_, _) => statuses.Add(label.Text);
            // Missing output directory stops the real method before any cloud client is constructed.
            var item = new QueueItem(Path.Combine(directory, "missing", "test.wav"), MediaKind.Audio);
            var row = (ListViewItem)typeof(MainForm).GetMethod("CreateQueueRow", BindingFlags.NonPublic | BindingFlags.Static)!
                .Invoke(null, [item])!;
            var queue = (ListView)GetField(form, "queueList");
            queue.Items.Add(row);
            var task = (Task)typeof(MainForm).GetMethod("StartCloudBatchAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(form, [new PasteDecision(true, true, true, false)])!;
            var deadline = DateTime.UtcNow.AddSeconds(10);
            while (!task.IsCompleted && DateTime.UtcNow < deadline)
            {
                Application.DoEvents();
                Thread.Sleep(5);
            }
            Assert.IsTrue(task.IsCompleted);
            task.GetAwaiter().GetResult();
            var status = string.Join("\n", statuses);
            StringAssert.Contains(status, "自动继续腾讯云上传和识别");
            if (usedHours >= 10) StringAssert.Contains(status, "10 小时参考线");
            Assert.AreEqual(1, notifications);
            Assert.IsFalse((bool)GetField(form, "isProcessing"));
            Assert.IsFalse(File.Exists(paths.JobsFile));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    private static object GetField(MainForm form, string name) =>
        typeof(MainForm).GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(form)!;

    private static void SetField(MainForm form, string name, object value) =>
        typeof(MainForm).GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(form, value);

    private sealed class FakeProbe : IMediaProbe
    {
        public Task<MediaProbeResult> ProbeAsync(string sourcePath, CancellationToken cancellationToken) =>
            Task.FromResult(new MediaProbeResult("wav", TimeSpan.FromMinutes(1), false, 1, "pcm_s16le", 22050, 1,
                new Dictionary<string, string>()));
    }
}
