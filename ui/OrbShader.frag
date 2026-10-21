#version 440
// GIDEON Orb — Qt 6 ShaderEffect Fragment Shader.
// Boreal Pine & Bioluminescent Canopy theme for GIDEON Prime.
// Dynamic speech acoustics, vocal harmonic ripples, and multi-tier state support.
// Compile before use: /usr/lib/qt6/bin/qsb --qt6 -o OrbShader.frag.qsb OrbShader.frag

layout(location = 0) in vec2 qt_TexCoord0;
layout(location = 0) out vec4 fragColor;

layout(std140, binding = 0) uniform buf {
    mat4  qt_Matrix;
    float qt_Opacity;
    float u_time;         // seconds, free-running (drives noise drift)
    float u_rms;          // 0..1 smoothed mic/voice energy
    float u_state_morph;  // 0 = calm sphere, 1 = fully energetic fluid
    float u_swirl;        // accumulated rotation angle of the lobes (radians)
    float u_hue;          // accumulated palette cycle position
    float u_radius;       // orb radius as a fraction of the half-window
    float u_speak;        // 0..1 weight of voice-driven harmonic waves
    float u_hotmic;       // 0..1 amber/sun halo strength
    vec4  u_color1;       // Primary halo / outer glow
    vec4  u_color2;       // Body / core tone
    vec4  u_color3;       // Deep shadow / floor tone
};

// ---- Simplex Noise 2D (Ashima / Ian McEwan, MIT) ---------------------------
vec3 permute(vec3 x) { return mod(((x * 34.0) + 1.0) * x, 289.0); }

float snoise(vec2 v) {
    const vec4 C = vec4(0.211324865405187, 0.366025403784439,
                        -0.577350269189626, 0.024390243902439);
    vec2 i  = floor(v + dot(v, C.yy));
    vec2 x0 = v - i + dot(i, C.xx);
    vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
    vec4 x12 = x0.xyxy + C.xxzz;
    x12.xy -= i1;
    i = mod(i, 289.0);
    vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));
    vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.0);
    m = m * m;
    m = m * m;
    vec3 x  = 2.0 * fract(p * C.www) - 1.0;
    vec3 h  = abs(x) - 0.5;
    vec3 ox = floor(x + 0.5);
    vec3 a0 = x - ox;
    m *= 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h);
    vec3 g;
    g.x  = a0.x * x0.x + h.x * x0.y;
    g.yz = a0.yz * x12.xz + h.yz * x12.yw;
    return 130.0 * dot(m, g);
}

// Circular 3-colour palette; x wraps every 3.0
vec3 pal(float x) {
    x = mod(x, 3.0);
    float f = smoothstep(0.0, 1.0, fract(x));
    int k = int(floor(x));
    vec3 a = (k == 0) ? u_color1.rgb : (k == 1) ? u_color2.rgb : u_color3.rgb;
    vec3 b = (k == 0) ? u_color2.rgb : (k == 1) ? u_color3.rgb : u_color1.rgb;
    return mix(a, b, f);
}

float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

void main() {
    vec2  p   = (qt_TexCoord0 - 0.5) * 2.0;     // -1..1 across the window
    float t   = u_time;
    float e   = clamp(u_state_morph, 0.0, 1.0);
    float rms = clamp(u_rms, 0.0, 1.0);
    float r   = length(p);
    float ang = atan(p.y, p.x);
    vec2  dir = vec2(cos(ang), sin(ang));       // seamless around the wrap

    // ---- Silhouette: organic noise wobble + vocal harmonic dynamics ----------
    float n1 = snoise(dir * 1.35 + vec2(t * 0.22, u_swirl * 0.11));
    float n2 = snoise(dir * 3.1 - vec2(t * 0.38, 3.7));

    float deform = n1 * 0.030 + n2 * 0.012;
    deform += e * (n1 * 0.052 + n2 * 0.030);

    // Dynamic, pronounced vocal formant distortion when speaking
    float speakHarmonics = 0.60 * sin(ang * 5.0 + t * 4.8 + u_swirl * 1.6)
                         + 0.40 * cos(ang * 3.0 - t * 3.4);
    float speechPulse = sin(ang * 8.0 - t * 6.2) * 0.08;
    deform += u_speak * (0.04 * sin(t * 5.0) + (speakHarmonics + speechPulse) * (0.09 + 0.26 * rms));
    deform += (1.0 - u_speak) * rms * n1 * 0.05;

    float R = u_radius * (1.0 + deform);
    float d = r / R;
    float body = 1.0 - smoothstep(0.955, 1.0, d);

    // ---- Interior: warped organic lobe field -------------------------------
    vec2 q = p / R;
    float warpAmt = 0.10 + 0.20 * e + 0.12 * rms + 0.08 * u_speak;
    q += warpAmt * vec2(snoise(q * 1.45 + vec2(t * 0.28, 0.0)),
                        snoise(q * 1.45 + vec2(5.2, -t * 0.25)));

    float lobeSize = 0.54 + 0.10 * rms + 0.06 * e + 0.08 * u_speak;
    vec3  lobes  = vec3(0.0);
    float weight = 0.0;
    for (int i = 0; i < 3; i++) {
        float fi = float(i);
        float a  = u_swirl + fi * 2.0943951 + 0.5 * sin(t * 0.7 + fi * 1.7);
        float orbit = 0.34 + 0.10 * sin(t * 0.9 + fi * 2.1) + 0.12 * e + 0.10 * rms + 0.08 * u_speak;
        vec2  c  = orbit * vec2(cos(a), sin(a));
        float dd = dot(q - c, q - c);
        float f  = exp(-dd / (lobeSize * lobeSize));
        lobes  += pal(fi + u_hue) * f;
        weight += f;
    }
    vec3 lobeCol = lobes / (weight + 0.08);
    float lobeAmt = clamp(weight, 0.0, 1.0);

    // ---- 3D Shading & Bioluminescent Core ----------------------------------
    vec2  qd = clamp(p / R, vec2(-1.0), vec2(1.0));
    float z  = sqrt(max(1.0 - dot(qd, qd), 0.0));
    vec3  N  = normalize(vec3(qd, z + 0.15));
    vec3  L  = normalize(vec3(-0.45, 0.55, 0.70));
    float diff = 0.55 + 0.45 * max(dot(N, L), 0.0);
    float fres = pow(1.0 - z, 2.4);
    float spec = pow(max(dot(N, normalize(L + vec3(0.0, 0.0, 1.0))), 0.0), 48.0);

    float coreFall = exp(-dot(p / R, p / R) * 3.2);
    // Bioluminescent inner core tinted with bright flora tone & vocal brightness
    vec3  coreCol  = mix(u_color2.rgb, mix(u_color1.rgb, vec3(1.0), 0.5), 0.45);
    float voiceFlare = u_speak * rms * (0.45 + 0.30 * sin(t * 8.0));
    float coreAmt  = coreFall * (0.30 + 0.85 * rms + 0.25 * e + voiceFlare);

    vec3 orb = lobeCol * (0.28 + 0.72 * lobeAmt) * diff;
    orb += coreCol * coreAmt;
    orb += mix(u_color1.rgb, u_color2.rgb, 0.5) * fres * (0.70 + 0.9 * rms + 0.35 * u_speak);
    orb += vec3(1.0) * spec * (0.14 + 0.10 * e + 0.12 * u_speak);
    orb = orb / (1.0 + 0.30 * orb);              // soft shoulder, no clipping

    // ---- Outer Bloom (Atmospheric Canopy Glow) -----------------------------
    float outside = max(r - R, 0.0) / u_radius;
    float bloomA  = exp(-outside * 3.3) * (0.20 + 0.45 * rms + 0.15 * e + 0.25 * u_speak);
    bloomA *= smoothstep(1.0, 0.72, r) * (1.0 - body);
    vec3  bloomCol = mix(u_color1.rgb, u_color2.rgb, 0.5);

    // ---- Sun-Dappled / Amber Halo (Hot-Mic / Open Channel) ----------------
    float ringR = u_radius * 1.20 + u_radius * 0.02 * sin(t * 2.0 + ang * 2.0);
    float dr    = r - ringR;
    float ring  = exp(-(dr * dr) / 0.00018);
    float haze  = exp(-(dr * dr) / 0.0040) * 0.22;
    float shimmer = 0.78 + 0.22 * sin(ang * 2.0 + t * 1.4);
    float haloA = (ring * 0.95 + haze) * shimmer * u_hotmic;
    haloA *= smoothstep(1.0, 0.85, r);
    vec3  haloCol = vec3(0.96, 0.74, 0.20); // Warm sun-dappled gold

    // ---- Premultiplied Alpha Composite ------------------------------------
    float alpha = clamp(body + bloomA + haloA, 0.0, 1.0);
    vec3  rgb   = orb * body + bloomCol * bloomA + haloCol * haloA;
    rgb += (hash12(gl_FragCoord.xy + fract(t)) - 0.5) / 255.0 * alpha; // anti-banding
    rgb = min(max(rgb, vec3(0.0)), vec3(alpha));

    fragColor = vec4(rgb, alpha) * qt_Opacity;
}
