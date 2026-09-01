using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class CloudJobProcessorTests
{
    [TestMethod]
    public async Task Video_CustomOutputDirectoryKeepsSourceAndPairsMp3WithTxt()
    {
        var directory = CreateTemporaryDirectory();
        try
        {
            var sourceDirectory = Path.Combine(directory, "source");
            var outputDirectory = Path.Combine(directory, "output");
            Directory.CreateDirectory(sourceDirectory);
            Directory.CreateDirectory(outputDirectory);
            var sourcePath = Path.Combine(sourceDirectory, "课程.mp4");
            await File.WriteAllTextAsync(sourcePath, "source-stays-read-only");
            var dependencies = CreateDependencies(
                directory,
                CloudTranscriptionState.Succeeded,
                "识别正文",
                hasVideo: true);
            var processor = dependencies.CreateProcessor();

            var result = await processor.ProcessVideoAsync(
                new QueueItem(sourcePath, MediaKind.Video),
                192,
                outputDirectory,
                progress: null,
                CancellationToken.None);

            Assert.AreEqual(Path.Combine(outputDirectory, "课程.mp3"), result.Mp3Path);
            Assert.AreEqual(Path.Combine(outputDirectory, "课程.txt"), result.TxtPath);
            Assert.IsTrue(File.Exists(result.Mp3Path));
            Assert.IsTrue(File.Exists(result.TxtPath));
            Assert.AreEqual("source-stays-read-only", await File.ReadAllTextAsync(sourcePath));
            Assert.IsFalse(File.Exists(Path.Combine(sourceDirectory, "课程.mp3")));
            Assert.IsFalse(File.Exists(Path.Combine(sourceDirectory, "课程.txt")));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [TestMethod]
    public async Task ExistingMp3_SuccessWritesBodyDeletesCloudObjectAndLeavesSourceUnchanged()
    {
        var directory = CreateTemporaryDirectory();
        try
        {
            var sourcePath = Path.Combine(directory, "现有音频.mp3");
            var originalBytes = new byte[] { 1, 2, 3, 4, 5 };
            await File.WriteAllBytesAsync(sourcePath, originalBytes);
            var dependencies = CreateDependencies(directory, CloudTranscriptionState.Succeeded, "第一句。\n第二句！");
            var processor = dependencies.CreateProcessor();
            var item = new QueueItem(sourcePath, MediaKind.Mp3);

            var result = await processor.ProcessMp3Async(item, directory, progress: null, CancellationToken.None);

            CollectionAssert.AreEqual(originalBytes, await File.ReadAllBytesAsync(sourcePath));
            Assert.IsTrue(result.TxtGenerated);
            Assert.IsNotNull(result.TxtPath);
            var txtBytes = await File.ReadAllBytesAsync(result.TxtPath);
            CollectionAssert.AreEqual(new byte[] { 0xEF, 0xBB, 0xBF }, txtBytes[..3]);
            StringAssert.Contains(await File.ReadAllTextAsync(result.TxtPath), "第一句。\r\n第二句！");
            Assert.AreEqual(1, dependencies.ObjectStore.DeleteCount);
            Assert.AreEqual(TimeSpan.FromMinutes(1), dependencies.UsageLedger.AddedDuration);
            Assert.IsEmpty(await dependencies.JobStore.LoadPendingAsync(CancellationToken.None));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [TestMethod]
    public async Task CancellationAfterSubmission_ThrowsContinuesAndPreservesRecoveryJobAndCloudObject()
    {
        var directory = CreateTemporaryDirectory();
        try
        {
            var sourcePath = Path.Combine(directory, "existing.mp3");
            await File.WriteAllBytesAsync(sourcePath, [1, 2, 3]);
            var dependencies = CreateDependencies(directory, CloudTranscriptionState.Running, null);
            using var cancellation = new CancellationTokenSource();
            var processor = dependencies.CreateProcessor((_, token) =>
            {
                cancellation.Cancel();
                return Task.FromCanceled(token);
            });

            var exception = await Assert.ThrowsExactlyAsync<CloudTaskContinuesException>(() =>
                processor.ProcessMp3Async(
                    new QueueItem(sourcePath, MediaKind.Mp3),
                    directory,
                    progress: null,
                    cancellation.Token));

            Assert.AreEqual("123456", exception.TaskId);
            Assert.AreEqual(0, dependencies.ObjectStore.DeleteCount);
            var pending = await dependencies.JobStore.LoadPendingAsync(CancellationToken.None);
            Assert.HasCount(1, pending);
            Assert.AreEqual(JobStage.CloudTranscribing, pending[0].Stage);
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [TestMethod]
    public async Task RecognitionFailure_DeletesCloudObjectAndRemovesRecoveryJob()
    {
        var directory = CreateTemporaryDirectory();
        try
        {
            var sourcePath = Path.Combine(directory, "existing.mp3");
            await File.WriteAllBytesAsync(sourcePath, [1, 2, 3]);
            var dependencies = CreateDependencies(directory, CloudTranscriptionState.Failed, null);
            var processor = dependencies.CreateProcessor();

            await Assert.ThrowsExactlyAsync<InvalidDataException>(() =>
                processor.ProcessMp3Async(
                    new QueueItem(sourcePath, MediaKind.Mp3),
                    directory,
                    progress: null,
                    CancellationToken.None));

            Assert.AreEqual(1, dependencies.ObjectStore.DeleteCount);
            Assert.IsEmpty(await dependencies.JobStore.LoadPendingAsync(CancellationToken.None));
            Assert.IsEmpty(Directory.GetFiles(directory, "*.txt"));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    private static TestDependencies CreateDependencies(
        string directory,
        CloudTranscriptionState transcriptionState,
        string? transcript,
        bool hasVideo = false)
    {
        var probe = new FakeProbe(hasVideo);
        var converter = new FakeConverter();
        var committer = new AtomicOutputCommitter();
        var objectStore = new FakeObjectStore();
        var transcriber = new FakeTranscriber(transcriptionState, transcript);
        var jobStore = new MemoryJobStore();
        var usageLedger = new MemoryUsageLedger();
        var paths = new LocalDataPaths(Path.Combine(directory, "local-data"));
        return new TestDependencies(
            probe,
            converter,
            new LocalVideoProcessor(probe, converter, committer),
            committer,
            objectStore,
            transcriber,
            jobStore,
            usageLedger,
            paths);
    }

    private static string CreateTemporaryDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), $"AccessibleVideoToText.CloudTests.{Guid.NewGuid():N}");
        Directory.CreateDirectory(path);
        return path;
    }

    private sealed record TestDependencies(
        FakeProbe Probe,
        FakeConverter Converter,
        LocalVideoProcessor LocalProcessor,
        AtomicOutputCommitter Committer,
        FakeObjectStore ObjectStore,
        FakeTranscriber Transcriber,
        MemoryJobStore JobStore,
        MemoryUsageLedger UsageLedger,
        LocalDataPaths Paths)
    {
        public CloudJobProcessor CreateProcessor(Func<TimeSpan, CancellationToken, Task>? delay = null) => new(
            Probe,
            Converter,
            LocalProcessor,
            Committer,
            ObjectStore,
            Transcriber,
            JobStore,
            UsageLedger,
            Paths,
            new CloudCredentials("1250000000", "test-credential-id", "test-credential-key"),
            delay: delay);
    }

    private sealed class FakeProbe(bool hasVideo) : IMediaProbe
    {
        public Task<MediaProbeResult> ProbeAsync(string sourcePath, CancellationToken cancellationToken) =>
            Task.FromResult(new MediaProbeResult(
                "mp3",
                TimeSpan.FromMinutes(1),
                HasVideo: hasVideo,
                AudioStreamCount: 1,
                AudioCodec: "mp3",
                SampleRate: 44100,
                Channels: 2,
                new Dictionary<string, string>()));
    }

    private sealed class FakeConverter : IAudioConverter
    {
        public async Task ConvertFinalMp3Async(
            AudioConversionRequest request,
            IProgress<int>? progress,
            CancellationToken cancellationToken)
        {
            await File.WriteAllBytesAsync(request.TemporaryOutputPath, [1, 2, 3], cancellationToken);
            progress?.Report(100);
        }

        public async Task CreateCloudAudioAsync(CloudAudioRequest request, IProgress<int>? progress, CancellationToken cancellationToken)
        {
            await File.WriteAllBytesAsync(request.TemporaryOutputPath, [9, 8, 7], cancellationToken);
            progress?.Report(100);
        }
    }

    private sealed class FakeObjectStore : ICloudObjectStore
    {
        public int DeleteCount { get; private set; }

        public Task<string> EnsureReadyAsync(CancellationToken cancellationToken) =>
            Task.FromResult("accessible-video-to-text-1250000000");

        public Task<CloudObjectReference> UploadAsync(string localPath, IProgress<int>? progress, CancellationToken cancellationToken)
        {
            progress?.Report(100);
            return Task.FromResult(new CloudObjectReference(
                "accessible-video-to-text-1250000000",
                "ap-shanghai",
                "accessible-video-to-text/00000000000000000000000000000000.mp3",
                new Uri("https://example.invalid/random.mp3?signature=redacted")));
        }

        public Task DeleteAsync(CloudObjectReference cloudObject, CancellationToken cancellationToken)
        {
            DeleteCount++;
            return Task.CompletedTask;
        }
    }

    private sealed class FakeTranscriber(CloudTranscriptionState state, string? transcript) : ICloudTranscriber
    {
        public Task<string> SubmitAsync(Uri signedAudioUrl, CancellationToken cancellationToken) => Task.FromResult("123456");

        public Task<CloudTranscriptionStatus> GetStatusAsync(string taskId, CancellationToken cancellationToken) =>
            Task.FromResult(new CloudTranscriptionStatus(
                state,
                transcript,
                state == CloudTranscriptionState.Failed ? "recognition-failed" : null,
                state == CloudTranscriptionState.Failed ? "腾讯云录音识别失败。" : null));
    }

    private sealed class MemoryJobStore : IJobStore
    {
        private IReadOnlyList<CloudRecoveryJob> jobs = [];

        public Task<IReadOnlyList<CloudRecoveryJob>> LoadPendingAsync(CancellationToken cancellationToken) => Task.FromResult(jobs);

        public Task SavePendingAsync(IReadOnlyCollection<CloudRecoveryJob> newJobs, CancellationToken cancellationToken)
        {
            jobs = newJobs.ToArray();
            return Task.CompletedTask;
        }
    }

    private sealed class MemoryUsageLedger : IUsageLedger
    {
        public TimeSpan AddedDuration { get; private set; }

        public Task<TimeSpan> GetCurrentMonthUsageAsync(string secretId, CancellationToken cancellationToken) =>
            Task.FromResult(AddedDuration);

        public Task AddSuccessfulDurationAsync(string secretId, TimeSpan duration, CancellationToken cancellationToken)
        {
            AddedDuration += duration;
            return Task.CompletedTask;
        }
    }
}
