using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class OutputDirectoryPolicyTests
{
    private string testDirectory = null!;

    [TestInitialize]
    public void Initialize()
    {
        testDirectory = Path.Combine(
            Path.GetTempPath(),
            "AccessibleVideoToText.OutputPolicyTests",
            Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(testDirectory);
    }

    [TestCleanup]
    public void Cleanup()
    {
        var expectedRoot = Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.OutputPolicyTests");
        if (testDirectory.StartsWith(expectedRoot, StringComparison.OrdinalIgnoreCase) && Directory.Exists(testDirectory))
        {
            Directory.Delete(testDirectory, recursive: true);
        }
    }

    [TestMethod]
    public void ResolveForSource_DefaultUsesSourceDirectoryAndLeavesNoProbe()
    {
        var sourcePath = Path.Combine(testDirectory, "课程.mp4");
        File.WriteAllText(sourcePath, "source");

        var result = OutputDirectoryPolicy.ResolveForSource(new AppSettings(), sourcePath);

        Assert.AreEqual(Path.GetFullPath(testDirectory), result);
        Assert.IsEmpty(Directory.GetFiles(testDirectory, ".feichuan-output.*.write-test"));
    }

    [TestMethod]
    public void ResolveForSource_CustomUsesNormalizedExistingDirectory()
    {
        var sourceDirectory = Path.Combine(testDirectory, "source");
        var outputDirectory = Path.Combine(testDirectory, "output");
        Directory.CreateDirectory(sourceDirectory);
        Directory.CreateDirectory(outputDirectory);
        var sourcePath = Path.Combine(sourceDirectory, "课程.mp4");
        File.WriteAllText(sourcePath, "source");
        var settings = new AppSettings(
            OutputPreference: OutputDirectoryPreference.CustomDirectory,
            CustomOutputDirectory: Path.Combine(outputDirectory, "."));

        var result = OutputDirectoryPolicy.ResolveForSource(settings, sourcePath);

        Assert.AreEqual(Path.GetFullPath(outputDirectory), result);
        Assert.IsEmpty(Directory.GetFiles(outputDirectory, ".feichuan-output.*.write-test"));
    }

    [TestMethod]
    public void UnknownPreferenceUsesSafeDefault()
    {
        Assert.AreEqual(
            OutputDirectoryPreference.SourceDirectory,
            OutputDirectoryPolicy.NormalizePreference("future-value"));
        Assert.IsFalse(OutputDirectoryPolicy.UsesCustomDirectory(
            new AppSettings(OutputPreference: "future-value", CustomOutputDirectory: testDirectory)));
    }

    [TestMethod]
    public void CustomPreferenceRejectsMissingDirectory()
    {
        var sourcePath = Path.Combine(testDirectory, "课程.mp4");
        File.WriteAllText(sourcePath, "source");
        var settings = new AppSettings(
            OutputPreference: OutputDirectoryPreference.CustomDirectory,
            CustomOutputDirectory: Path.Combine(testDirectory, "missing"));

        var exception = Assert.ThrowsExactly<InvalidDataException>(() =>
            OutputDirectoryPolicy.ResolveForSource(settings, sourcePath));

        StringAssert.Contains(exception.Message, "重新选择结果目录");
    }

    [TestMethod]
    public void ValidateCustomDirectoryRejectsFilePath()
    {
        var filePath = Path.Combine(testDirectory, "not-a-directory.txt");
        File.WriteAllText(filePath, "data");

        Assert.ThrowsExactly<InvalidDataException>(() =>
            OutputDirectoryPolicy.ValidateCustomDirectory(filePath));
    }
}
