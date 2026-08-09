using System.Diagnostics;
using System.Security.Cryptography;
using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class FfmpegIntegrationTests
{
    public TestContext TestContext { get; set; } = null!;

    [TestMethod]
    [TestCategory("Integration")]
    [DataRow(128)]
    [DataRow(192)]
    [DataRow(256)]
    [DataRow(320)]
    public async Task ConvertFinalMp3_UsesRequestedCbrFirstAudioAndNoVideo(int bitrateKbps)
    {
        if (!await CanStartAsync("ffmpeg", "-version"))
        {
            Assert.Inconclusive("当前环境找不到 FFmpeg，跳过本机集成测试。 ");
        }

        var testDirectory = Path.Combine(
            Path.GetTempPath(),
            "AccessibleVideoToText.Tests",
            $"ffmpeg-{bitrateKbps}-{Guid.NewGuid():N}");
        Directory.CreateDirectory(testDirectory);
        try
        {
            var sourcePath = Path.Combine(testDirectory, "中文 多音轨(样例).mkv");
            await GenerateTwoAudioTrackVideoAsync(sourcePath);
            var beforeHash = await ComputeSha256Async(sourcePath);
            var localPaths = new LocalDataPaths(Path.Combine(testDirectory, "local-data"));
            var probe = new FfmpegMediaProbe(localPaths);
            var processor = new LocalVideoProcessor(
                probe,
                new FfmpegAudioConverter(localPaths),
                new AtomicOutputCommitter());
            var item = new QueueItem(sourcePath, MediaKind.Video);

            var result = await processor.ProcessAsync(item, bitrateKbps, progress: null, CancellationToken.None);

            var outputProbe = await probe.ProbeAsync(result.Mp3Path, CancellationToken.None);
            var afterHash = await ComputeSha256Async(sourcePath);
            CollectionAssert.AreEqual(beforeHash, afterHash, "源视频不得被修改。 ");
            Assert.AreEqual(2, result.ProbeResult.AudioStreamCount, "测试视频必须包含两条音轨。 ");
            Assert.AreEqual(1, outputProbe.AudioStreamCount);
            Assert.IsFalse(outputProbe.HasVideo, "MP3 不得嵌入视频流或封面流。 ");
            Assert.AreEqual("mp3", outputProbe.AudioCodec);
            Assert.IsTrue(
                outputProbe.BitRate >= bitrateKbps * 1000 * 0.80 &&
                outputProbe.BitRate <= bitrateKbps * 1000 * 1.20,
                $"探测码率 {outputProbe.BitRate} 不接近请求的 {bitrateKbps} kbps。 ");
            Assert.AreEqual("无障碍测试标题", outputProbe.Metadata.GetValueOrDefault("title"));
            Assert.AreEqual("测试艺术家", outputProbe.Metadata.GetValueOrDefault("artist"));
            Assert.AreEqual("测试专辑", outputProbe.Metadata.GetValueOrDefault("album"));
            Assert.IsEmpty(Directory.GetFiles(testDirectory, "*.part.mp3"));
        }
        finally
        {
            if (testDirectory.StartsWith(Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests"), StringComparison.OrdinalIgnoreCase) &&
                Directory.Exists(testDirectory))
            {
                Directory.Delete(testDirectory, recursive: true);
            }
        }
    }

    private static async Task GenerateTwoAudioTrackVideoAsync(string outputPath)
    {
        var arguments = new[]
        {
            "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1.5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1.5",
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0",
            "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
            "-metadata", "title=无障碍测试标题",
            "-metadata", "artist=测试艺术家",
            "-metadata", "album=测试专辑",
            outputPath
        };

        var (exitCode, error) = await RunProcessAsync("ffmpeg", arguments, TimeSpan.FromSeconds(30));
        Assert.AreEqual(0, exitCode, $"生成集成测试视频失败：{error}");
    }

    private static async Task<byte[]> ComputeSha256Async(string path)
    {
        await using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            4096,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        return await SHA256.HashDataAsync(stream);
    }

    private static async Task<bool> CanStartAsync(string fileName, string argument)
    {
        try
        {
            var (exitCode, _) = await RunProcessAsync(fileName, [argument], TimeSpan.FromSeconds(10));
            return exitCode == 0;
        }
        catch (Exception exception) when (exception is System.ComponentModel.Win32Exception or FileNotFoundException)
        {
            return false;
        }
    }

    private static async Task<(int ExitCode, string Error)> RunProcessAsync(
        string fileName,
        IEnumerable<string> arguments,
        TimeSpan timeout)
    {
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = fileName,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardError = true,
                RedirectStandardOutput = true
            }
        };
        foreach (var argument in arguments)
        {
            process.StartInfo.ArgumentList.Add(argument);
        }

        process.Start();
        var errorTask = process.StandardError.ReadToEndAsync();
        var outputTask = process.StandardOutput.ReadToEndAsync();
        using var timeoutSource = new CancellationTokenSource(timeout);
        await process.WaitForExitAsync(timeoutSource.Token);
        await outputTask;
        return (process.ExitCode, await errorTask);
    }
}
