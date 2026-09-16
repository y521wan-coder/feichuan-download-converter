using System.Diagnostics;
using System.Media;
using System.Text;

namespace AccessibleVideoToText.App;

// Scopes stay on the UI thread. Nested download/conversion stages form one task.
public sealed class TaskCompletionNotifier(Action play)
{
    private readonly Action playSound = play;
    private int depth;

    public IDisposable Begin()
    {
        depth++;
        return new Scope(this);
    }

    private sealed class Scope(TaskCompletionNotifier owner) : IDisposable
    {
        private bool disposed;

        public void Dispose()
        {
            if (disposed) return;
            disposed = true;
            if (--owner.depth != 0) return;
            try
            {
                owner.playSound();
            }
            catch (Exception)
            {
                // Audio must never change a task result or interrupt UI cleanup.
                Trace.TraceWarning("Task completion sound could not be played.");
            }
        }
    }
}

internal static class CompletionSound
{
    private static readonly Lazy<SoundPlayer> fallback = new(() =>
    {
        var sound = new SoundPlayer(new MemoryStream(CreateWave(), writable: false));
        sound.Load();
        return sound;
    });
    private static readonly Lazy<SoundPlayer> player = new(() =>
        LoadPlayer(Path.Combine(AppContext.BaseDirectory, "任务结束.wav")));

    private static SoundPlayer LoadPlayer(string path)
    {
        try
        {
            // Load a private memory copy so users can replace the WAV without a file lock.
            var bytes = File.ReadAllBytes(path);
            if (bytes.Length < 44 || !bytes.AsSpan(0, 4).SequenceEqual("RIFF"u8) ||
                !bytes.AsSpan(8, 4).SequenceEqual("WAVE"u8))
                throw new InvalidDataException("Invalid completion WAV header.");
            var custom = new SoundPlayer(new MemoryStream(bytes, writable: false));
            try
            {
                custom.Load();
                return custom;
            }
            catch
            {
                custom.Stream?.Dispose();
                custom.Dispose();
                throw;
            }
        }
        catch (Exception)
        {
            Trace.TraceWarning("Custom completion WAV unavailable; using the built-in chime.");
        }
        var sound = new SoundPlayer(new MemoryStream(CreateWave(), writable: false));
        sound.Load();
        return sound;
    }

    public static void Play()
    {
        try { player.Value.Play(); }
        catch (Exception)
        {
            Trace.TraceWarning("Custom completion WAV cannot play; using the built-in chime.");
            fallback.Value.Play();
        }
    }

    // Original, softly fading three-note chime, also used as a fallback.
    private static byte[] CreateWave()
    {
        const int sampleRate = 22050;
        const int noteSamples = sampleRate / 4;
        double[] notes = [659.25, 830.61, 987.77];
        var dataSize = noteSamples * notes.Length * sizeof(short);
        using var stream = new MemoryStream(44 + dataSize);
        using var writer = new BinaryWriter(stream, Encoding.ASCII, leaveOpen: true);
        writer.Write(Encoding.ASCII.GetBytes("RIFF"));
        writer.Write(36 + dataSize);
        writer.Write(Encoding.ASCII.GetBytes("WAVEfmt "));
        writer.Write(16);
        writer.Write((short)1);
        writer.Write((short)1);
        writer.Write(sampleRate);
        writer.Write(sampleRate * sizeof(short));
        writer.Write((short)sizeof(short));
        writer.Write((short)16);
        writer.Write(Encoding.ASCII.GetBytes("data"));
        writer.Write(dataSize);
        foreach (var frequency in notes)
        {
            for (var index = 0; index < noteSamples; index++)
            {
                var time = (double)index / sampleRate;
                var envelope = Math.Min(1, time / 0.012) *
                    Math.Min(1, (double)(noteSamples - 1 - index) / (sampleRate * 0.06)) *
                    Math.Exp(-6 * time);
                writer.Write((short)(short.MaxValue * 0.24 * envelope *
                    Math.Sin(2 * Math.PI * frequency * time)));
            }
        }
        return stream.ToArray();
    }
}
