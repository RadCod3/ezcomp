"""GLSL for the compositor: a fullscreen-triangle pass combining two textures."""

VERTEX = """
#version 330 core
out vec2 v_uv;
void main() {
    vec2 p = vec2(float((gl_VertexID & 1) << 2) - 1.0,
                  float((gl_VertexID & 2) << 1) - 1.0);
    v_uv = (p + 1.0) * 0.5;
    gl_Position = vec4(p, 0.0, 1.0);
}
"""

FRAGMENT = """
#version 330 core
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D texA;
uniform sampler2D texB;
uniform int   uMode;   // 0 A, 1 B, 2 wipe, 3 diff, 4 onion
uniform float uParam;  // wipe pos | diff gain | onion mix
void main() {
    vec3 a = texture(texA, v_uv).rgb;
    vec3 b = texture(texB, v_uv).rgb;
    vec3 c;
    if (uMode == 0) {
        c = a;
    } else if (uMode == 1) {
        c = b;
    } else if (uMode == 2) {                       // wipe
        c = (v_uv.x < uParam) ? a : b;
        if (abs(v_uv.x - uParam) < 0.0015) c = vec3(1.0, 0.85, 0.0);
    } else if (uMode == 3) {                        // amplified difference
        c = clamp(abs(a - b) * uParam, 0.0, 1.0);
    } else {                                        // onion-skin
        c = mix(a, b, uParam);
    }
    fragColor = vec4(c, 1.0);
}
"""
