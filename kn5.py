"""Standalone reader for Assetto Corsa .kn5 files.

Format (version 5-6):
  "sc6969", u32 version, [u32 extra_len + bytes if version > 5]
  u32 texture_count, then per texture: u32 type, str name, u32 size, bytes
  u32 material_count, then per material: see read_material
  node tree, depth first
"""
import struct, sys, os, math

class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v

    def u16(self):
        v = struct.unpack_from('<H', self.d, self.p)[0]; self.p += 2; return v

    def u32(self):
        v = struct.unpack_from('<I', self.d, self.p)[0]; self.p += 4; return v

    def i32(self):
        v = struct.unpack_from('<i', self.d, self.p)[0]; self.p += 4; return v

    def f32(self):
        v = struct.unpack_from('<f', self.d, self.p)[0]; self.p += 4; return v

    def vec(self, n):
        v = struct.unpack_from('<%df' % n, self.d, self.p); self.p += 4 * n; return v

    def string(self):
        n = self.u32()
        s = self.d[self.p:self.p + n]; self.p += n
        return s.decode('utf-8', 'replace')

    def skip(self, n):
        self.p += n


class Material:
    __slots__ = ('name', 'shader', 'blend', 'alpha_tested', 'depth_mode', 'props', 'textures')

class Node:
    __slots__ = ('type', 'name', 'active', 'matrix', 'children', 'parent',
                 'material_id', 'vertex_count', 'index_count', 'layer',
                 'lod_in', 'lod_out', 'center', 'radius', 'cast_shadows',
                 'visible', 'transparent', 'renderable', 'verts', 'uvs', 'normals', 'indices')
    def __init__(self):
        self.children = []
        self.parent = None
        self.matrix = None
        self.material_id = None
        self.vertex_count = 0
        self.index_count = 0
        self.verts = None
        self.uvs = None
        self.normals = None
        self.indices = None


def placement_matrix(position, rotation):
    """models.ini POSITION/ROTATION as a kn5 row-vector matrix.

    ROTATION's first number is a turn about the vertical axis, in degrees.
    Measured on Highlands' drift layout, the one stock model placed this way:
    its start light gantry (100.kn5, ROTATION=-17) hangs 3.8 m over a flat
    road heading 20.3 degrees off the model's own axis. Read as this turn the
    lights end up 3.3 degrees off the road; with the opposite sign, 37. The
    other two numbers have no stock example to measure, so they are refused
    rather than guessed (see convert_track.layout_files).
    """
    yaw = math.radians(rotation[0])
    c, s = math.cos(yaw), math.sin(yaw)
    return [c, 0.0, s, 0.0,
            0.0, 1.0, 0.0, 0.0,
            -s, 0.0, c, 0.0,
            position[0], position[1], position[2], 1.0]


class KN5:
    def __init__(self, path, geometry=False, placement=None):
        self.geometry = geometry
        with open(path, 'rb') as f:
            data = f.read()
        self.size = len(data)
        r = Reader(data)
        sig = r.d[0:6]
        if sig != b'sc6969':
            raise ValueError('not a kn5 file, signature was %r' % sig)
        r.skip(6)
        self.version = r.u32()
        if self.version > 5:
            extra = r.u32()
            if extra:
                r.skip(extra)
        self.textures = []
        for _ in range(r.u32()):
            ttype = r.u32()
            name = r.string()
            size = r.u32()
            r.skip(size)
            self.textures.append((name, ttype, size))
        self.materials = []
        for _ in range(r.u32()):
            self.materials.append(self.read_material(r))
        self.root = self.read_node(r, None)
        self.consumed = r.p
        if placement:
            # Where the track's models.ini puts this whole file. A parent
            # above the root, so every world transform walking up the parents
            # picks it up and walk() still starts at the file's own root.
            top = Node()
            top.type = 1
            top.name = "__placement__"
            top.matrix = placement_matrix(*placement)
            top.children = [self.root]
            self.root.parent = top

    def read_material(self, r):
        m = Material()
        m.name = r.string()
        m.shader = r.string()
        m.blend = r.u8()
        m.alpha_tested = r.u8()
        m.depth_mode = r.u32() if self.version > 4 else 0
        m.props = {}
        for _ in range(r.u32()):
            pname = r.string()
            value = r.f32()
            r.vec(2)   # unused vec2 slot
            r.vec(3)   # unused vec3 slot
            r.vec(4)   # unused vec4 slot
            m.props[pname] = value
        m.textures = {}
        for _ in range(r.u32()):
            sample = r.string()
            slot = r.u32()
            tex = r.string()
            m.textures[sample] = tex
        return m

    def read_node(self, r, parent):
        n = Node()
        n.type = r.u32()
        n.name = r.string()
        child_count = r.u32()
        n.active = r.u8()
        n.parent = parent
        if n.type == 1:
            n.matrix = r.vec(16)
        elif n.type in (2, 3):
            if n.type == 3:
                # skinned: bone table then per bone matrices
                bones = r.u32()
                for _ in range(bones):
                    r.string()
                    r.vec(16)
            n.cast_shadows = r.u8()
            n.visible = r.u8()
            n.transparent = r.u8()
            n.vertex_count = r.u32()
            # position 3f, normal 3f, uv 2f, tangent 3f
            stride = 44 if n.type == 2 else 76
            if self.geometry:
                raw = r.d[r.p:r.p + n.vertex_count * stride]
                r.skip(n.vertex_count * stride)
                n.verts = [struct.unpack_from('<3f', raw, i * stride)
                           for i in range(n.vertex_count)]
                n.uvs = [struct.unpack_from('<2f', raw, i * stride + 24)
                         for i in range(n.vertex_count)]
                n.normals = [struct.unpack_from('<3f', raw, i * stride + 12)
                             for i in range(n.vertex_count)]
            else:
                r.skip(n.vertex_count * stride)
            n.index_count = r.u32()
            if self.geometry:
                n.indices = struct.unpack_from('<%dH' % n.index_count, r.d, r.p)
                r.skip(n.index_count * 2)
            else:
                r.skip(n.index_count * 2)
            n.material_id = r.u32()
            n.layer = r.u32()
            n.lod_in = r.f32()
            n.lod_out = r.f32()
            n.center = r.vec(3)
            n.radius = r.f32()
            n.renderable = r.u8()
        else:
            raise ValueError('unknown node type %d at %s' % (n.type, n.name))
        for _ in range(child_count):
            n.children.append(self.read_node(r, n))
        return n

    def walk(self, node=None):
        node = self.root if node is None else node
        yield node
        for c in node.children:
            yield from self.walk(c)


if __name__ == '__main__':
    k = KN5(sys.argv[1])
    nodes = list(k.walk())
    meshes = [n for n in nodes if n.type in (2, 3)]
    print('version      %d' % k.version)
    print('bytes read   %d of %d  (%s)' % (
        k.consumed, k.size, 'exact' if k.consumed == k.size else 'MISMATCH'))
    print('textures     %d' % len(k.textures))
    print('materials    %d' % len(k.materials))
    print('nodes        %d  (%d meshes)' % (len(nodes), len(meshes)))
    print('triangles    %d' % (sum(n.index_count for n in meshes) // 3))
