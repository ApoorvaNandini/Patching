#!/usr/bin/env python

# Copyright (c) 2019 Computer Vision Center (CVC) at the Universitat Autonoma de
# Barcelona (UAB).
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

import glob
import os
import sys
import csv

try:
    sys.path.append(glob.glob('../carla/dist/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass

import carla
from agents.navigation.roaming_agent import RoamingAgent
from agents.navigation.basic_agent import BasicAgent
from agents.navigation.behavior_agent import BehaviorAgent
from agents.navigation.global_route_planner import GlobalRoutePlanner
from agents.navigation.global_route_planner_dao import GlobalRoutePlannerDAO

from agents.navigation.controller import VehiclePIDController

from buffered_saver_lc import BufferedImageSaver
from agents.tools.misc import get_speed

import random
from PIL import Image
import scipy.misc as misc
import math
import logging

try:
    import pygame
    from pygame.locals import KMOD_CTRL
    from pygame.locals import K_ESCAPE
    from pygame.locals import K_q
    from pygame.locals import K_LEFT
    from pygame.locals import K_RIGHT
    from pygame.locals import K_m
    from pygame.locals import K_o
    from pygame.locals import K_a
    from pygame.locals import K_l
    from pygame.locals import K_r
    from pygame.locals import K_s
    from pygame.locals import K_j
    from pygame.locals import K_p
    from pygame.locals import K_q
    from pygame.locals import K_c
    from pygame.locals import K_d
    from pygame.locals import K_g
    from pygame.locals import K_t
    from pygame.locals import K_u
    from pygame.locals import K_b
    from pygame.locals import K_SPACE
    from pygame.locals import K_UP
    from pygame.locals import K_DOWN
except ImportError:
    raise RuntimeError('cannot import pygame, make sure pygame package is installed')

try:
    import numpy as np
except ImportError:
    raise RuntimeError('cannot import numpy, make sure numpy package is installed')

try:
    import queue
except ImportError:
    import Queue as queue


class CarlaSyncMode(object):
    """
    Context manager to synchronize output from different sensors. Synchronous
    mode is enabled as long as we are inside this context

        with CarlaSyncMode(world, sensors) as sync_mode:
            while True:
                data = sync_mode.tick(timeout=1.0)

    """

    def __init__(self, world, *sensors, **kwargs):
        self.world = world
        self.sensors = sensors
        self.frame = None
        self.delta_seconds = 1.0 / kwargs.get('fps', 20)
        self._queues = []
        self._settings = None

    def __enter__(self):
        self._settings = self.world.get_settings()
        self.frame = self.world.apply_settings(carla.WorldSettings(
            no_rendering_mode=False,
            synchronous_mode=True,
            fixed_delta_seconds=self.delta_seconds))

        def make_queue(register_event):
            q = queue.Queue()
            register_event(q.put)
            self._queues.append(q)

        make_queue(self.world.on_tick)
        for sensor in self.sensors:
            make_queue(sensor.listen)
        return self

    def tick(self, timeout):
        self.frame = self.world.tick()
        data = [self._retrieve_data(q, timeout) for q in self._queues]
        assert all(x.frame == self.frame for x in data)
        return data

    def __exit__(self, *args, **kwargs):
        self.world.apply_settings(self._settings)

    def _retrieve_data(self, sensor_queue, timeout):
        while True:
            data = sensor_queue.get(timeout=timeout)
            if data.frame == self.frame:
                return data


# ==============================================================================
# -- KeyboardControl -----------------------------------------------------------
# ==============================================================================


class KeyboardControl(object):
    def __init__(self, world, vehicle, autopilot_enabled=True):
        self.vehicle = vehicle
        self.autopilot_enabled = autopilot_enabled
        self.control = carla.VehicleControl()
        self.steer_cache = 0.0
        self.left_lane_change_activated = 0
        self.right_lane_change_activated = 0
        self.lane_change_second_half = 0
        self.start_data_collection = False
        self.force_left_lane_change = False
        self.force_right_lane_change = False
        self.junk = 0
        self.get_waypoint = False
        self.spawn_static_object = False
        self.destroy_static_object = False
        self.print_next_loc = False

    def parse_events(self, clock):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return True
            if event.type == pygame.KEYDOWN:
                if event.key == K_m:
                    self.autopilot_enabled = False
                    self.vehicle.set_autopilot(self.autopilot_enabled)
                    print('Autopilot Off')
                if event.key == K_o:
                    self.autopilot_enabled = True
                    self.vehicle.set_autopilot(self.autopilot_enabled)
                    print('Autopilot On; Lane change deactivated!')
                if event.key == K_l:
                    self.left_lane_change_activated = 1
                    self.lane_change_second_half = -1
                    print('Left lane change activated')
                if event.key == K_r:
                    self.right_lane_change_activated = 1
                    self.lane_change_second_half = -1
                    print('Right lane change activated')
                if event.key == K_s:
                    self.lane_change_second_half = 1
                    print('Second half of lane change')
                if event.key == K_c:
                    self.start_data_collection = True
                    print('Starting data collection')
                if event.key == K_p:
                    self.start_data_collection = False
                    print('Pausing data collection')
                if event.key == K_a:
                    print("Forcing left lane change")
                    self.force_left_lane_change = True
                if event.key == K_d:
                    print("Forcing right lane change")
                    self.force_right_lane_change = True
                if event.key == K_q:
                    print("lane change over")
                    self.left_lane_change_activated = 0
                    self.right_lane_change_activated = 0
                    self.lane_change_second_half = 0
                if event.key == K_t:
                    print("spawn static object")
                    self.spawn_static_object = True
                if event.key == K_u:
                    print("destroying static object")
                    self.destroy_static_object = True
                if event.key == K_g:
                    print("getting waypoint")
                    self.get_waypoint = True
                if event.key == K_b:
                    print("print next 1 meter location")
                    self.print_next_loc = True

            if event.type == pygame.KEYUP:
                if self._is_quit_shortcut(event.key):
                    return True

        if not self.autopilot_enabled:
            self._parse_vehicle_keys(pygame.key.get_pressed(), clock.get_time())
            self.control.reverse = self.control.gear < 0          
            self.vehicle.apply_control(self.control)

    def _parse_vehicle_keys(self, keys, milliseconds):
        self.control.throttle = 1.0 if keys[K_UP] else 0.0
        steer_increment = 5e-4 * milliseconds
        if keys[K_LEFT]:
            if self.steer_cache > 0:
                self.steer_cache = 0
            else:
                self.steer_cache -= steer_increment
        elif keys[K_RIGHT]:
            if self.steer_cache < 0:
                self.steer_cache = 0
            else:
                self.steer_cache += steer_increment
        else:
            self.steer_cache = 0.0
        self.steer_cache = min(0.7, max(-0.7, self.steer_cache))
        self.control.steer = round(self.steer_cache, 1)
        self.control.brake = 1.0 if keys[K_DOWN] else 0.0
        self.control.hand_brake = keys[K_SPACE]

    @staticmethod
    def _is_quit_shortcut(key):
        return (key == K_ESCAPE) or (key == K_q and pygame.key.get_mods() & KMOD_CTRL)

# ==============================================================================
# ------------------------------------------------------------------------------
# ==============================================================================

def draw_image(surface, image, blend=False):
    array = np.frombuffer(image.raw_data, dtype=np.dtype("uint8"))
    array = np.reshape(array, (image.height, image.width, 4))
    array = array[:, :, :3]
    array = array[:, :, ::-1]
    image_surface = pygame.surfarray.make_surface(array.swapaxes(0, 1))
    if blend:
        image_surface.set_alpha(100)
    surface.blit(image_surface, (0, 0))
    return image.raw_data, array

def draw_image2(surface, image, blend=False):
    array = np.frombuffer(image.raw_data, dtype=np.dtype("uint8"))
    array = np.reshape(array, (image.height, image.width, 4))
    array = array[:, :, :3]
    array = array[:, :, ::-1]
    return image.raw_data, array


def get_font():
    fonts = [x for x in pygame.font.get_fonts()]
    default_font = 'ubuntumono'
    font = default_font if default_font in fonts else fonts[0]
    font = pygame.font.match_font(font)
    return pygame.font.Font(font, 14)

def draw_waypoints(world, waypoints, z=0.5):
    """
    Draw a list of waypoints at a certain height given in z.
        :param world: carla.world object
        :param waypoints: list or iterable container with the waypoints to draw
        :param z: height in meters
    """
    for wpt in waypoints:
        wpt_t = wpt.transform
        begin = wpt_t.location + carla.Location(z=z)
        angle = math.radians(wpt_t.rotation.yaw)
        end = begin + carla.Location(x=math.cos(angle), y=math.sin(angle))
        world.debug.draw_arrow(begin, end, arrow_size=0.3, life_time=10.0)

class get_displacement_in_polyline():
    def __init__(self, loc1, loc2, loc3, wps, distance=5):
        print("length of wps: ", len(wps))
        self.initial_position = loc1
        self.distance = distance
        self.locs_list = []
        lc_loc2 = self.translate(loc2, loc1)
        
        self.theta = math.atan2(lc_loc2.y, lc_loc2.x)
        cos_t = math.cos(self.theta)
        sin_t = math.sin(self.theta)
        
        lc_loc = self.transform(loc1)
        self.locs_list.append(lc_loc)
        lc_loc = self.transform(loc2)
        self.locs_list.append(lc_loc)
        lc_loc = self.transform(loc3)
        self.locs_list.append(lc_loc)
        
        for i in range(len(wps)):
            lc_loc = self.transform(wps[i].transform.location)
            self.locs_list.append(lc_loc)

        #self.locs_list = [self.lc_loc1, self.lc_loc2, self.lc_loc3, self.lc_loc4]
        
        print(self.locs_list[0].x, self.locs_list[0].y, self.locs_list[0].z)
        print(self.locs_list[1].x, self.locs_list[1].y, self.locs_list[1].z)
        print(self.locs_list[2].x, self.locs_list[2].y, self.locs_list[2].z)
        print(self.locs_list[3].x, self.locs_list[3].y, self.locs_list[3].z)
        print(self.locs_list[4].x, self.locs_list[4].y, self.locs_list[4].z)
        print(self.locs_list[5].x, self.locs_list[5].y, self.locs_list[5].z)
        print(self.locs_list[-1].x, self.locs_list[-1].y, self.locs_list[-1].z)
        #sys.stop()
        
        self.nxt_pointer = 1
        #self.max_pointer = 32
        self.crossed_pointer = 0

    def translate(self, loc, init_loc):
        return loc - init_loc
        
   
    def compute_distance(self, loc1, loc2):
        vector = loc1 - loc2
        distance = np.sqrt(vector.x**2 + vector.y**2)
        return distance

    def compute_polyline_distance(self, loc, cr_pt, nxt_pt):
        if cr_pt + 1 == nxt_pt:
            d = self.compute_distance(loc, self.locs_list[nxt_pt])
        else:
            d = self.compute_distance(loc, self.locs_list[cr_pt+1])
            for pt_idx in range(cr_pt+1, nxt_pt):
                d += self.compute_distance(self.locs_list[pt_idx], self.locs_list[pt_idx+1])
        return d

    def find_x_image_on_line(self, crossed_loc, next_loc, cu_loc):
        m = float(crossed_loc.y - next_loc.y)/float(crossed_loc.x - next_loc.x)
        c = float(crossed_loc.x * next_loc.y - next_loc.x * crossed_loc.y)/float(crossed_loc.x - next_loc.x)
        x = cu_loc.x
        y = m * x + c
        return (x, y)

    def transform(self, loc):
        local_loc = loc - self.initial_position
        cos_t = math.cos(self.theta)
        sin_t = math.sin(self.theta)
     
        if self.theta > 0.0 or self.theta < -0.0:
            tmp_x = local_loc.x * cos_t + local_loc.y * sin_t
            tmp_y = - local_loc.x * sin_t + local_loc.y * cos_t
            local_loc.x = tmp_x
            local_loc.y = tmp_y
        return local_loc

    def find_point_on_line(self, initial_loc, terminal_loc, distance):
        v = np.array([initial_loc.x, initial_loc.y], dtype=float)
        u = np.array([terminal_loc.x, terminal_loc.y], dtype=float)
        n = v - u
        n /= np.linalg.norm(n, 2)
        point = v - distance * n

        #print("initial loc: ", initial_loc)
        #print("terminal loc: ", terminal_loc)
        #print("middle point: ", point)

        return tuple(point)


def main():
    data_path = '/home/apoorva/data/test/'
    lane_change_number = 0
    BIS = BufferedImageSaver(data_path, 300, 800, 600, 3, 'CameraRGB', lane_change_number)

    actor_list = []
    pygame.init()

    display = pygame.display.set_mode(
        (800, 600),
        pygame.HWSURFACE | pygame.DOUBLEBUF)
    font = get_font()
    clock = pygame.time.Clock()

    client = carla.Client('localhost', 2000)
    client.set_timeout(2.0)
    world = client.get_world()#load_world('Town04')
    tm = client.get_trafficmanager(3000)
    tm.set_synchronous_mode(True)
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
    tot_target_reached = 0
    num_min_waypoints = 5
    obstacle = None

    csv_filename = '/home/apoorva/lc-data/data.csv'

    fields = ['crossed_pointer', 'nxt_pointer', 'cu_loc_x', 'cu_loc_y', 'gt_x', 'gt_y',
              'target_loc_x', 'target_loc_y', 'dy', 'steering']

    try:
        m = world.get_map()
        spawn_points = m.get_spawn_points()
        start_pose = spawn_points[64] #141
        end_location = random.choice(spawn_points).location 
        #carla.Location(x=-74.650337, y=141.064636, z=0.000000)
        blueprint_library = world.get_blueprint_library()

        vehicle = world.spawn_actor(
            random.choice(blueprint_library.filter('vehicle.audi.a2')),
            start_pose)
        actor_list.append(vehicle)
        world.player = vehicle
        tm.ignore_lights_percentage(vehicle,100)
        tm.auto_lane_change(vehicle, True)
        agent = BehaviorAgent(vehicle, ignore_traffic_light=True, behavior='cautious')
       
        agent.set_destination(agent.vehicle.get_location(), end_location,
                              clean=True)

        camera_rgb = world.spawn_actor(
            blueprint_library.find('sensor.camera.rgb'),
            carla.Transform(carla.Location(x=2.5, z=1.5)),
            attach_to=vehicle)
        actor_list.append(camera_rgb)

        camera_top_view = world.spawn_actor(
            blueprint_library.find('sensor.camera.rgb'),
            carla.Transform(carla.Location(x=-5.5, z=2.8),
                            carla.Rotation(pitch=-15)),
            attach_to=vehicle)
        actor_list.append(camera_top_view)

        controller = KeyboardControl(world, vehicle, True)
        polyline_controller = False

        with open(csv_filename, 'w') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames = fields)  
            writer.writeheader()

            # Create a synchronous mode context.
            with CarlaSyncMode(world, camera_rgb, camera_top_view, fps=30) as sync_mode:
                while True:
                    if controller.parse_events(clock):
                        return

                    agent.update_information(world)

                    clock.tick()
                    # Advance the simulation and wait for the data.
                    snapshot, image_rgb, image_topview = sync_mode.tick(timeout=2.0)
                    # Draw the display.
                    raw, img = draw_image2(display, image_rgb)
                    raw2, img2 = draw_image(display, image_topview)
                    fps = round(1.0 / snapshot.timestamp.delta_seconds)
                    

                    if vehicle.is_at_traffic_light():
                        traffic_light = vehicle.get_traffic_light()
                        if traffic_light.get_state() == carla.TrafficLightState.Red:
                            traffic_light.set_state(carla.TrafficLightState.Green)
                            traffic_light.set_green_time(10.0)

                    # Set new destination when target has been reached
                    if len(agent.get_local_planner().waypoints_queue) < num_min_waypoints:
                        agent.reroute(spawn_points)
                        tot_target_reached += 1
                        print("ReRouting")

                    if controller.spawn_static_object:
                        controller.spawn_static_object = False
                        ego_vehicle_loc = vehicle.get_location()
                        ego_vehicle_wp = m.get_waypoint(ego_vehicle_loc)
                        obstacle_wp = list(ego_vehicle_wp.next(30))[0]
                        obstacle_location = obstacle_wp.transform.location
                        obstacle = world.spawn_actor(
                            random.choice(blueprint_library.filter('vehicle.audi.a2')),
                            obstacle_wp.transform)
                        obstacle.set_location(obstacle_location)
                        obstacle.set_simulate_physics(False)
                        actor_list.append(obstacle)
  
                    if obstacle:
                        ego_vehicle_loc = vehicle.get_location()
                        vector = ego_vehicle_loc - obstacle_location
                        distance = np.sqrt(vector.x**2 + vector.y**2 + vector.z**2)
                        if distance > 27 and distance < 28:
                            print(distance)
                    else:
                        distance = 1000

                    if controller.destroy_static_object:
                        controller.destroy_static_object = False
                        obstacle.destroy()
                        obstacle = None


                    if controller.print_next_loc:
                        l1 = vehicle.get_location()
                        print("current location: ", l1)
                        w1 = m.get_waypoint(l1)
                        w2 = list(w1.next(1))[0]
                        
                        l2 = w2.transform.location
                        print("next 1m away loation: ", l2)
                        print((l2.x-l1.x), (l2.y-l1.y), (l2.z-l1.z))
                        print((l2.x-l1.x)**2+(l2.y-l1.y)**2+(l2.z-l1.z)**2)
                        controller.print_next_loc = False
 
                    if controller.force_left_lane_change:
                        print("Left Here")
                        controller.force_left_lane_change = False
                        ego_vehicle_loc = vehicle.get_location()
                        ego_vehicle_wp = m.get_waypoint(ego_vehicle_loc)
                        ego_vehicle_nxt_wp = list(ego_vehicle_wp.next(10))[0]
                        left_nxt_wpt = ego_vehicle_nxt_wp.get_left_lane()
                        left_nxt_nxt_wpt = list(left_nxt_wpt.next(15))[0]
                        
                        left_nxt_nxt_nxt_wpts = []
                        for wpt_i in range(30):
                            if wpt_i == 0:
                                tmp_wpt = list(left_nxt_nxt_wpt.next(1))[0]
                            else:
                                tmp_wpt = list(tmp_wpt.next(1))[0]
                            left_nxt_nxt_nxt_wpts.append(tmp_wpt)
                        #left_nxt_nxt_nxt_wpts = list(left_nxt_nxt_wpt.next_until_lane_end(1))#[:30]
                        len_nxt_nxt_nxt_wpts = min(30, len(left_nxt_nxt_nxt_wpts))
                        left_nxt_nxt_nxt_wpts = left_nxt_nxt_nxt_wpts[:len_nxt_nxt_nxt_wpts]

                        lc_waypoints_count = 3 + len_nxt_nxt_nxt_wpts  
                        wps = [ego_vehicle_wp, ego_vehicle_nxt_wp, left_nxt_nxt_wpt,
                               left_nxt_nxt_nxt_wpts[-1]]                     
                        if left_nxt_wpt is not None:
                            pl = get_displacement_in_polyline(ego_vehicle_loc,
                                                              ego_vehicle_nxt_wp.transform.location,
                                                              left_nxt_nxt_wpt.transform.location,
                                                              left_nxt_nxt_nxt_wpts,
                                                              distance=1)
                            draw_waypoints(world, wps , z=0.0)
                            polyline_controller = True

                    if controller.force_right_lane_change:
                        print("Right Here")
                        controller.force_right_lane_change = False
                        ego_vehicle_loc = vehicle.get_location()
                        ego_vehicle_wp = m.get_waypoint(ego_vehicle_loc)
                        ego_vehicle_nxt_wp = list(ego_vehicle_wp.next(10))[0]
                        right_nxt_wpt = ego_vehicle_nxt_wp.get_right_lane()
                        right_nxt_nxt_wpt = list(right_nxt_wpt.next(15))[0]
                        right_nxt_nxt_nxt_wpts = []
                        for wpt_i in range(30):
                            if wpt_i == 0:
                                tmp_wpt = list(right_nxt_nxt_wpt.next(1))[0]
                            else:
                                tmp_wpt = list(tmp_wpt.next(1))[0]
                            right_nxt_nxt_nxt_wpts.append(tmp_wpt)
                        #right_nxt_nxt_nxt_wpts = list(right_nxt_nxt_wpt.next_until_lane_end(1))#[:30]
                        len_nxt_nxt_nxt_wpts = min(30, len(right_nxt_nxt_nxt_wpts))
                        right_nxt_nxt_nxt_wpts = right_nxt_nxt_nxt_wpts[:len_nxt_nxt_nxt_wpts]
                        lc_waypoints_count = 3 + len_nxt_nxt_nxt_wpts
                        
                        wps = [ego_vehicle_wp, ego_vehicle_nxt_wp,
                               right_nxt_nxt_wpt, right_nxt_nxt_nxt_wpts[-1]]
                        if right_nxt_wpt is not None:
                            pl = get_displacement_in_polyline(ego_vehicle_loc,
                                                              ego_vehicle_nxt_wp.transform.location,
                                                              right_nxt_nxt_wpt.transform.location,
                                                              right_nxt_nxt_nxt_wpts,
                                                              distance=1)
                            draw_waypoints(world, wps , z=0.0)
                            polyline_controller = True
                            

                    if controller.autopilot_enabled:
                        if polyline_controller == True:
                            cu_tr = vehicle.get_transform()
                            # vehicle.bounding_box.extent
                            # bb_v[4], bb_v[6] are front bottom left and right locations of ego-car. 
                            bb_v = vehicle.bounding_box.get_world_vertices(cu_tr) 

                            local_bb_v4 = pl.transform(bb_v[4])
                            local_bb_v6 = pl.transform(bb_v[6])
                             
                            cu_loc = vehicle.get_location() # world co-ordinates
                            local_cu_loc = pl.transform(cu_loc)
                            # shifting local_cu_loc from center to front left bottom location
                            local_cu_loc.x = (local_bb_v4.x+local_bb_v6.x)/2
                            local_cu_loc.y = (local_bb_v4.y+local_bb_v6.y)/2
                            local_cu_loc.z = (local_bb_v4.z+local_bb_v6.z)/2
                            
                            if local_cu_loc.x >= pl.locs_list[pl.crossed_pointer+1].x:   
                                pl.crossed_pointer += 1

                            d1 = 0
                            while pl.compute_polyline_distance(
                                local_cu_loc, pl.crossed_pointer, pl.nxt_pointer) <= pl.distance:

                                if not pl.nxt_pointer == lc_waypoints_count - 1:
                                    # d1 is distance along polyline
                                    d1 = pl.compute_polyline_distance(
                                        local_cu_loc, pl.crossed_pointer, pl.nxt_pointer)
                                    pl.nxt_pointer += 1
                                    
                                else:
                                    d1 = pl.compute_polyline_distance(
                                        local_cu_loc, pl.crossed_pointer, pl.nxt_pointer)
                                    polyline_controller = False
                                    break

                            # XXX the ego vehicle may not lie exactly on line
                            gt_point = pl.find_x_image_on_line(
                                pl.locs_list[pl.crossed_pointer],
                                pl.locs_list[pl.crossed_pointer+1], 
                                local_cu_loc)

                            if d1 == 0:
                                point = pl.find_point_on_line(local_cu_loc, 
                                                              pl.locs_list[pl.nxt_pointer],
                                                              pl.distance)
                            else:
                                point = pl.find_point_on_line(pl.locs_list[pl.nxt_pointer - 1],
                                                              pl.locs_list[pl.nxt_pointer],
                                                              pl.distance - d1)

                            dy =  local_cu_loc.y - point[1]

                            steering =  - dy * 0.02
                            print(dy, steering)
                            control = agent.run_step()
                            control.steer = steering
                            vehicle.apply_control(control)
                            control_values = vehicle.get_control()

                            row = [{'crossed_pointer':pl.crossed_pointer,
                                    'nxt_pointer':pl.nxt_pointer,
                                    'cu_loc_x':local_cu_loc.x,
                                    'cu_loc_y':local_cu_loc.y,
                                    'gt_x':gt_point[0],
                                    'gt_y':gt_point[1], 
                                    'target_loc_x':point[0],
                                    'target_loc_y':point[1], 
                                    'dy':dy,
                                    'steering':steering}]
                            writer.writerows(row)

                        else:
                            control = agent.run_step()
                            #control.throttle = 0.5 * control.throttle
                            vehicle.apply_control(control)
                            control_values = vehicle.get_control()

                    if controller.get_waypoint:
                        location = vehicle.get_location()
                        ego_vehicle_wp = m.get_waypoint(location)
                        next_loc = list(ego_vehicle_wp.next(10))[0].transform.location
                        vehicle.set_location(next_loc)
                        controller.get_waypoint = False
 
                    v = vehicle.get_velocity()
   
                    display.blit(
                        font.render('% 5d FPS (real)' % clock.get_fps(), True, (255, 255, 255)), (8, 10))
                    display.blit(
                        font.render('% 5d FPS (simulated)' % fps, True, (255, 255, 255)), (8, 28))
                    display.blit(
                        font.render('% 5f speed (ego-car)' % (3.6 * math.sqrt(v.x**2 + v.y**2 + v.z**2)), True, (255, 255, 255)),
                        (8, 48))
                    pygame.display.flip()


                    

                    if controller.start_data_collection:
                        if BIS.index % 50 == 0:
                            print(BIS.index)
                        BIS.add_image(raw,
                                      control_values.steer,
                                      controller.left_lane_change_activated,
                                      controller.right_lane_change_activated,
                                      controller.lane_change_second_half,
                                      controller.junk,
                                      distance,
                                      'CameraRGB')

    finally:
        print('destroying actors.')
        for actor in actor_list:
            actor.destroy()

        pygame.quit()
        print('done.')


if __name__ == '__main__':

    try:

        main()

    except KeyboardInterrupt:
        print('\nCancelled by user. Bye!')