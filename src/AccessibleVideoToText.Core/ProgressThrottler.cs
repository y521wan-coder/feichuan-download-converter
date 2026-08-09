namespace AccessibleVideoToText.Core;

public sealed class ProgressThrottler
{
    private readonly int step;
    private int lastReported = -1;

    public ProgressThrottler(int step = 5)
    {
        if (step is < 1 or > 100)
        {
            throw new ArgumentOutOfRangeException(nameof(step));
        }

        this.step = step;
    }

    public int? Accept(double rawPercentage)
    {
        var clamped = (int)Math.Clamp(Math.Floor(rawPercentage), 0, 100);
        var bucket = clamped == 100 ? 100 : clamped / step * step;
        if (bucket <= lastReported)
        {
            return null;
        }

        lastReported = bucket;
        return bucket;
    }
}

