using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class LocalVideoProcessorTests
{
    private string testDirectory = null!;

    [TestInitialize]
    public void Initialize()
    {
        testDirectory = Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(testDirectory);
    }

    [TestCleanup]
    public void Cleanup()
    {
        if (testDirectory.StartsWith(Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests"), StringComparison.OrdinalIgnoreCase) &&
            Directory.Exists(testDirectory))
        {
            Directory.Delete(testDirectory, recursive: true);
        }
    }

    [TestMethod]
    public async Task ProcessAsync_WritesPartThenCommitsWithoutChangingSource()
    {
        var sourcePath = Path.Combine(testDirectory, "课程.mp4");
        await File.WriteAllTextAsync(sourcePath, "source-stays-read-only");
        var sourceBefore = await File.ReadAllTextAsync(sourcePath);
        var item = new QueueItem(sourcePath, MediaKind.Video);
        var converter = new FakeConverter();
        var processor = new LocalVideoProcessor(
            new FakeProbe(hasVideo: true, audioStreams: 2),
            converter,
            new AtomicOutputCommitter());

        var result = await processor.ProcessAsync(item, 192, progress: null, CancellationToken.None);

        Assert.IsTrue(File.Exists(result.Mp3Path));
        Assert.AreEqual("fake-mp3", await File.ReadAllTextAsync(result.Mp3Path));
        Assert.AreEqual(sourceBefore, await File.ReadAllTextAsync(sourcePath));
        Assert.AreEqual(JobStage.Succeeded, item.Stage);
        Assert.AreEqual(192, converter.LastRequest?.BitrateKbps);
        StringAssert.Contains(item.StepDetail, "源文件旁边");
    }

    [TestMethod]
    public async Task ProcessAsync_LeavesNoPartWhenConversionFails()
    {
        var sourcePath = Path.Combine(testDirectory, "损坏.mp4");
        await File.WriteAllTextAsync(sourcePath, "source");
        var processor = new LocalVideoProcessor(
            new FakeProbe(hasVideo: true, audioStreams: 1),
            new FakeConverter(shouldFail: true),
            new AtomicOutputCommitter());

        await Assert.ThrowsExactlyAsync<IOException>(() => processor.ProcessAsync(
            new QueueItem(sourcePath, MediaKind.Video),
            192,
            progress: null,
            CancellationToken.None));

        Assert.IsEmpty(Directory.GetFiles(testDirectory, "*.part.mp3"));
        Assert.IsFalse(File.Exists(Path.Combine(testDirectory, "损坏.mp3")));
    }

    [TestMethod]
    public async Task ProcessAsync_RejectsVideoWithoutAudio()
    {
        var sourcePath = Path.Combine(testDirectory, "无声.mp4");
        await File.WriteAllTextAsync(sourcePath, "source");
        var processor = new LocalVideoProcessor(
            new FakeProbe(hasVideo: true, audioStreams: 0),
            new FakeConverter(),
            new AtomicOutputCommitter());

        var exception = await Assert.ThrowsExactlyAsync<InvalidDataException>(() => processor.ProcessAsync(
            new QueueItem(sourcePath, MediaKind.Video),
            192,
            progress: null,
            CancellationToken.None));

        StringAssert.Contains(exception.Message, "没有可用音轨");
    }

    [TestMethod]
    public async Task ProcessAsync_ConvertsPureAudioWithoutRequiringVideoTrack()
    {
        var sourcePath = Path.Combine(testDirectory, "背景音乐.m4a");
        await File.WriteAllTextAsync(sourcePath, "source-audio-stays-read-only");
        var sourceBefore = await File.ReadAllTextAsync(sourcePath);
        var item = new QueueItem(sourcePath, MediaKind.Audio);
        var converter = new FakeConverter();
        var processor = new LocalVideoProcessor(
            new FakeProbe(hasVideo: false, audioStreams: 1),
            converter,
            new AtomicOutputCommitter());

        var result = await processor.ProcessAsync(item, 192, progress: null, CancellationToken.None);

        Assert.IsTrue(File.Exists(result.Mp3Path));
        Assert.AreEqual(MediaKind.Audio, item.Kind);
        Assert.AreEqual(sourceBefore, await File.ReadAllTextAsync(sourcePath));
        Assert.AreEqual(sourcePath, converter.LastRequest?.SourcePath);
    }

    private sealed class FakeProbe(bool hasVideo, int audioStreams) : IMediaProbe
    {
        public Task<MediaProbeResult> ProbeAsync(string sourcePath, CancellationToken cancellationToken)
        {
            return Task.FromResult(new MediaProbeResult(
                "fake",
                TimeSpan.FromSeconds(10),
                hasVideo,
                audioStreams,
                audioStreams > 0 ? "aac" : null,
                audioStreams > 0 ? 48000 : null,
                audioStreams > 0 ? 2 : null,
                new Dictionary<string, string>()));
        }
    }

    private sealed class FakeConverter(bool shouldFail = false) : IAudioConverter
    {
        public AudioConversionRequest? LastRequest { get; private set; }

        public async Task ConvertFinalMp3Async(
            AudioConversionRequest request,
            IProgress<int>? progress,
            CancellationToken cancellationToken)
        {
            LastRequest = request;
            await File.WriteAllTextAsync(request.TemporaryOutputPath, "fake-mp3", cancellationToken);
            if (shouldFail)
            {
                throw new IOException("模拟转换失败");
            }
        }

        public Task CreateCloudAudioAsync(
            CloudAudioRequest request,
            IProgress<int>? progress,
            CancellationToken cancellationToken) => throw new NotSupportedException();
    }
}
