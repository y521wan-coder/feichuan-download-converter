using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class ProgressAndStageTests
{
    [TestMethod]
    public void ProgressThrottler_ReportsAtApproximatelyFivePercentSteps()
    {
        var throttler = new ProgressThrottler(5);
        var reported = new List<int>();

        foreach (var value in new[] { 0d, 1d, 4.9d, 5d, 7d, 10.1d, 99d, 100d })
        {
            if (throttler.Accept(value) is { } accepted)
            {
                reported.Add(accepted);
            }
        }

        CollectionAssert.AreEqual(new[] { 0, 5, 10, 95, 100 }, reported);
    }

    [TestMethod]
    public void OnlyDocumentedTerminalStagesAreTerminal()
    {
        var terminal = Enum.GetValues<JobStage>().Where(stage => stage.IsTerminal()).ToArray();

        CollectionAssert.AreEqual(
            new[] { JobStage.Succeeded, JobStage.PartiallySucceeded, JobStage.Failed, JobStage.Cancelled },
            terminal);
    }
}

