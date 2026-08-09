using System.Text;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class AtomicOutputCommitterTests
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
    public async Task WriteUtf8BomTextAsync_UsesBomAndWindowsLineEndings()
    {
        var finalPath = Path.Combine(testDirectory, "结果.txt");
        var committer = new AtomicOutputCommitter();

        await committer.WriteUtf8BomTextAsync(finalPath, "第一句。\n第二句。", CancellationToken.None);

        var bytes = await File.ReadAllBytesAsync(finalPath);
        CollectionAssert.AreEqual(Encoding.UTF8.Preamble.ToArray(), bytes.Take(3).ToArray());
        var text = Encoding.UTF8.GetString(bytes[3..]);
        Assert.AreEqual("第一句。\r\n第二句。", text);
        Assert.IsEmpty(Directory.GetFiles(testDirectory, "*.part.txt"));
    }

    [TestMethod]
    public async Task WriteUtf8BomTextAsync_RejectsEmptyRecognitionWithoutCreatingTxt()
    {
        var finalPath = Path.Combine(testDirectory, "空结果.txt");
        var committer = new AtomicOutputCommitter();

        await Assert.ThrowsExactlyAsync<InvalidDataException>(
            () => committer.WriteUtf8BomTextAsync(finalPath, " \r\n ", CancellationToken.None));

        Assert.IsFalse(File.Exists(finalPath));
    }

    [TestMethod]
    public async Task WriteUtf8BomTextAsync_NeverOverwritesAnExistingFile()
    {
        var finalPath = Path.Combine(testDirectory, "已有.txt");
        await File.WriteAllTextAsync(finalPath, "原内容");
        var committer = new AtomicOutputCommitter();

        await Assert.ThrowsExactlyAsync<IOException>(
            () => committer.WriteUtf8BomTextAsync(finalPath, "新内容", CancellationToken.None));

        Assert.AreEqual("原内容", await File.ReadAllTextAsync(finalPath));
    }
}

