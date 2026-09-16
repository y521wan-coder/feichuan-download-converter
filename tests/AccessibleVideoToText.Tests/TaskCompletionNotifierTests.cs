using AccessibleVideoToText.App;
using System.Media;
using System.Reflection;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class TaskCompletionNotifierTests
{
    [TestMethod]
    [DataRow("valid")]
    [DataRow("missing")]
    [DataRow("invalid")]
    public void ReplaceableWave_LoadsCustomBytesWithoutLockingAndFallsBackWhenUnavailable(string state)
    {
        var sound = typeof(MainForm).Assembly.GetType("AccessibleVideoToText.App.CompletionSound")!;
        var original = (byte[])sound.GetMethod("CreateWave", BindingFlags.NonPublic | BindingFlags.Static)!
            .Invoke(null, null)!;
        var custom = (byte[])original.Clone();
        custom[44] = 1;
        var directory = Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, "任务结束.wav");
        try
        {
            if (state == "valid") File.WriteAllBytes(path, custom);
            if (state == "invalid") File.WriteAllText(path, "not a WAV");
            using var player = (SoundPlayer)sound.GetMethod("LoadPlayer", BindingFlags.NonPublic | BindingFlags.Static)!
                .Invoke(null, [path])!;
            using var stream = player.Stream!;
            stream.Position = 0;
            using var loaded = new MemoryStream();
            stream.CopyTo(loaded);
            CollectionAssert.AreEqual(state == "valid" ? custom : original, loaded.ToArray());
            // The current sound must not retain a file handle or overwrite a custom file.
            if (state == "valid")
            {
                using var writable = File.Open(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                Assert.AreEqual(custom.Length, writable.Length);
            }
        }
        finally { Directory.Delete(directory, recursive: true); }
    }

    [TestMethod]
    public void DownloadAndPostProcessing_NotifyOnlyAfterWholeTaskEnds()
    {
        var count = 0;
        var notifier = new TaskCompletionNotifier(() => count++);
        var download = notifier.Begin();
        using (notifier.Begin()) { }
        Assert.AreEqual(0, count, "转换结束前不应播放下载结束音。");
        download.Dispose();
        download.Dispose();
        Assert.AreEqual(1, count);
        using (notifier.Begin()) { }
        Assert.AreEqual(2, count, "下一次独立任务仍应播放。");
    }

    [TestMethod]
    public async Task AsyncFailureAndCancellation_StillNotifyOnce()
    {
        var count = 0;
        var notifier = new TaskCompletionNotifier(() => count++);
        foreach (var error in new Exception[] { new IOException(), new OperationCanceledException() })
        {
            try
            {
                using var task = notifier.Begin();
                await Task.Yield();
                using var conversion = notifier.Begin();
                throw error;
            }
            catch (Exception caught) when (ReferenceEquals(caught, error)) { }
        }
        Assert.AreEqual(2, count);
    }

    [TestMethod]
    public void UnavailableAudio_DoesNotInterruptTaskOrFollowingNotifications()
    {
        var count = 0;
        var notifier = new TaskCompletionNotifier(() =>
        {
            count++;
            throw new InvalidOperationException("Audio unavailable");
        });
        using (notifier.Begin()) { }
        using (notifier.Begin()) { }
        Assert.AreEqual(2, count);
    }
}
